import { BN, Program } from "@coral-xyz/anchor";
import {
  Blockhash,
  ComputeBudgetProgram,
  PublicKey,
  SystemProgram,
  TransactionInstruction,
  TransactionMessage,
  VersionedTransaction,
} from "@solana/web3.js";
import { CustodyAccount, Position } from "../types";
import { Perpetuals } from "../idl/jupiter-perpetuals-idl";
import { generatePositionRequestPda } from "./generate-position-and-position-request-pda";
import {
  createAssociatedTokenAccountIdempotentInstruction,
  createCloseAccountInstruction,
  createSyncNativeInstruction,
  getAssociatedTokenAddressSync,
  NATIVE_MINT,
} from "@solana/spl-token";
import {
  JLP_POOL_ACCOUNT_PUBKEY,
  JUPITER_PERPETUALS_PROGRAM,
  JUPITER_PERPETUALS_PROGRAM_ID,
  RPC_CONNECTION,
} from "../constants";

export async function constructMarketOpenPositionTrade({
  custody,
  collateralCustody,
  collateralTokenDelta,
  inputMint,
  jupiterMinimumOut,
  owner,
  priceSlippage,
  program,
  recentBlockhash,
  side,
  sizeUsdDelta,
  positionPubkey,
}: {
  custody: CustodyAccount;
  collateralCustody: CustodyAccount;
  collateralTokenDelta: BN;
  inputMint: PublicKey;
  jupiterMinimumOut: BN | null;
  owner: PublicKey;
  priceSlippage: BN;
  program: Program<Perpetuals>;
  recentBlockhash: Blockhash;
  side: Position["side"];
  sizeUsdDelta: BN;
  positionPubkey: PublicKey;
}) {
  // The `positionRequest` PDA holds the requests for all the perpetuals actions. Once the `positionRequest`
  // is submitted on chain, the keeper(s) will pick them up and execute the requests (hence the request
  // fulfillment model)
  const { positionRequest, counter } = generatePositionRequestPda({
    positionPubkey,
    requestChange: "increase",
  });

  // The `positionRequestAta` accounts hold the user's input mint tokens, which will be swapped (if required)
  // and used to fund the collateral custody's token account when the instruction is executed
  const positionRequestAta = getAssociatedTokenAddressSync(
    inputMint,
    positionRequest,
    true,
  );

  // `fundingAccount` is the token account where we'll withdraw the `inputMint` from. Essentially, the flow of tokens will be:
  // `fundingAccount` -> `positionRequestAta` -> `collateralCustodyTokenAccount`
  const fundingAccount = getAssociatedTokenAddressSync(inputMint, owner);

  const preInstructions: TransactionInstruction[] = [];
  const postInstructions: TransactionInstruction[] = [];

  // Wrap to wSOL if needed so we can treat SOL as an SPL token
  // https://spl.solana.com/token#example-wrapping-sol-in-a-token
  if (inputMint.equals(NATIVE_MINT)) {
    const createWrappedSolAtaIx =
      createAssociatedTokenAccountIdempotentInstruction(
        owner,
        fundingAccount,
        owner,
        NATIVE_MINT,
      );

    preInstructions.push(createWrappedSolAtaIx);

    // Transfer SOL to the wSOL associated token account and use SyncNative below to update wrapped SOL balance
    preInstructions.push(
      SystemProgram.transfer({
        fromPubkey: owner,
        toPubkey: fundingAccount,
        lamports: BigInt(collateralTokenDelta.toString()),
      }),
    );

    preInstructions.push(createSyncNativeInstruction(fundingAccount));

    postInstructions.push(
      createCloseAccountInstruction(fundingAccount, owner, owner),
    );
  }

  const increaseIx = await program.methods
    .createIncreasePositionMarketRequest({
      counter,
      collateralTokenDelta,
      // jupiterMinimumOut is required for trades that require swaps
      // Call the Jupiter Quote API (https://station.jup.ag/api-v6/get-quote) to convert the `inputMintAmount`
      // to get the `jupiterMinimumOut` which is the required minimum token out amount when the swap is performed
      jupiterMinimumOut:
        jupiterMinimumOut && jupiterMinimumOut.gten(0)
          ? jupiterMinimumOut
          : null,
      priceSlippage,
      side,
      sizeUsdDelta,
    })
    .accounts({
      custody: custody.publicKey,
      collateralCustody: collateralCustody.publicKey,
      fundingAccount,
      inputMint,
      owner,
      perpetuals: PublicKey.findProgramAddressSync(
        [Buffer.from("perpetuals")],
        JUPITER_PERPETUALS_PROGRAM_ID,
      )[0],
      pool: JLP_POOL_ACCOUNT_PUBKEY,
      position: positionPubkey,
      positionRequest,
      positionRequestAta,
      referral: null,
    })
    .instruction();

  const instructions = [
    ComputeBudgetProgram.setComputeUnitPrice({
      microLamports: 100000, // Get the estimated compute unit price here from RPC or a provider like Triton
    }),
    ...preInstructions,
    increaseIx,
    ...postInstructions,
  ];

  const simulateTx = new VersionedTransaction(
    new TransactionMessage({
      instructions,
      // `payerKey` for simulation can be any account as long as it has enough SOL to cover the gas fees
      payerKey: PublicKey.default,
      // We don't need to pass in a real blockhash here since the `replaceRecentBlockhash`
      // option in `simulateTransaction` gets the latest blockhash from the RPC's internal cache
      // Reference: https://github.com/anza-xyz/agave/blob/master/rpc/src/rpc.rs#L3890-L3907
      recentBlockhash: PublicKey.default.toString(),
    }).compileToV0Message([]),
  );

  const simulation = await RPC_CONNECTION.simulateTransaction(simulateTx, {
    replaceRecentBlockhash: true,
    sigVerify: false,
  });

  instructions.unshift(
    ComputeBudgetProgram.setComputeUnitLimit({
      units: simulation.value.unitsConsumed || 1_400_000,
    }),
  );

  const txMessage = new TransactionMessage({
    payerKey: owner,
    recentBlockhash,
    instructions,
  }).compileToV0Message();

  // This transaction can be then signed and submitted onchain for the keeper to execute the trade
  // https://station.jup.ag/guides/perpetual-exchange/request-fulfillment-model
  const tx = new VersionedTransaction(txMessage);
}

export async function constructMarketClosePositionTrade({
  desiredMint,
  program,
  recentBlockhash,
  positionPubkey,
}: {
  desiredMint: PublicKey;
  program: Program<Perpetuals>;
  recentBlockhash: Blockhash;
  positionPubkey: PublicKey;
}) {
  const position =
    await JUPITER_PERPETUALS_PROGRAM.account.position.fetch(positionPubkey);

  // The `positionRequest` PDA holds the requests for all the perpetuals actions. Once the `positionRequest`
  // is submitted on chain, the keeper(s) will pick them up and execute the requests (hence the request
  // fulfillment model)
  const { positionRequest, counter } = generatePositionRequestPda({
    positionPubkey,
    requestChange: "decrease",
  });

  const preInstructions: TransactionInstruction[] = [];
  const postInstructions: TransactionInstruction[] = [];

  // `desiredMint` is the mint address of the token to receive when closing the position. It can either be the
  // mint address of the custody token itself (BTC/ETH/SOL) or the USDC mint
  //
  // `receivingAccount` will then be the owner's ATA for the `desiredMint`
  const receivingAccount = getAssociatedTokenAddressSync(
    desiredMint,
    position.owner,
    true,
  );

  if (desiredMint.equals(NATIVE_MINT)) {
    postInstructions.push(
      createCloseAccountInstruction(
        receivingAccount,
        position.owner,
        position.owner,
      ),
    );
  }

  const decreaseIx = await program.methods
    .createDecreasePositionMarketRequest({
      collateralUsdDelta: new BN(0),
      sizeUsdDelta: new BN(0),
      // `priceSlippage` here is scaled to 6 decimal places as per the USDC mint, so for example if the price of SOL is $100, the value would `new BN(100_000_000)`
      // For longs and for a lower chance of exceeding the price slippage, use a value is 5-10% lower than the current token price
      // For shorts and for a lower chance of exceeding the price slippage, use a value that is 5-10% higher than the current token price
      priceSlippage: new BN(100_000_000_000),
      // `jupiterMinimumOut` is not needed because we won't perform any token swaps when withdrawing collateral or reducing position size
      jupiterMinimumOut: null,
      counter,
      entirePosition: true,
    })
    .accounts({
      owner: position.owner,
      // The `receivingAccount` is the token account where we'll return the withdrawed collateral / tokens
      receivingAccount,
      perpetuals: PublicKey.findProgramAddressSync(
        [Buffer.from("perpetuals")],
        JUPITER_PERPETUALS_PROGRAM_ID,
      )[0],
      pool: JLP_POOL_ACCOUNT_PUBKEY,
      position: positionPubkey,
      positionRequest,
      // The `positionRequestAta` accounts hold the user's input mint tokens, which will be swapped (if required)
      // and used to fund the collateral custody's token account when the instruction is executed
      positionRequestAta: getAssociatedTokenAddressSync(
        desiredMint,
        positionRequest,
        true,
      ),
      custody: position.custody,
      collateralCustody: position.collateralCustody,
      desiredMint,
      referral: null,
    })
    .instruction();

  const instructions = [
    ComputeBudgetProgram.setComputeUnitPrice({
      microLamports: 100000, // Get the estimated compute unit price here from RPC or a provider like Triton
    }),
    ...preInstructions,
    decreaseIx,
    ...postInstructions,
  ];

  const simulateTx = new VersionedTransaction(
    new TransactionMessage({
      instructions,
      // `payerKey` for simulation can be any account as long as it has enough SOL to cover the gas fees
      payerKey: PublicKey.default,
      // We don't need to pass in a real blockhash here since the `replaceRecentBlockhash`
      // option in `simulateTransaction` gets the latest blockhash from the RPC's internal cache
      // Reference: https://github.com/anza-xyz/agave/blob/master/rpc/src/rpc.rs#L3890-L3907
      recentBlockhash: PublicKey.default.toString(),
    }).compileToV0Message([]),
  );

  const simulation = await RPC_CONNECTION.simulateTransaction(simulateTx, {
    replaceRecentBlockhash: true,
    sigVerify: false,
  });

  instructions.unshift(
    ComputeBudgetProgram.setComputeUnitLimit({
      units: simulation.value.unitsConsumed || 1_400_000,
    }),
  );

  const txMessage = new TransactionMessage({
    payerKey: position.owner,
    recentBlockhash,
    instructions,
  }).compileToV0Message();

  // This transaction can be then signed and submitted onchain for the keeper to execute the trade
  // https://station.jup.ag/guides/perpetual-exchange/request-fulfillment-model
  const tx = new VersionedTransaction(txMessage);
}
