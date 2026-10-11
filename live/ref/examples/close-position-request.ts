import {
  createAssociatedTokenAccountIdempotentInstruction,
  getAssociatedTokenAddressSync,
  NATIVE_MINT,
} from "@solana/spl-token";
import {
  JLP_POOL_ACCOUNT_PUBKEY,
  JUPITER_PERPETUALS_PROGRAM,
  RPC_CONNECTION,
} from "../constants";
import {
  ComputeBudgetProgram,
  Keypair,
  PublicKey,
  TransactionInstruction,
  TransactionMessage,
  VersionedTransaction,
} from "@solana/web3.js";
import { utils } from "@coral-xyz/anchor";

async function closePositionRequest(_positionRequestPubkey: string) {
  try {
    // Fetch position request (TPSL/LO) account
    const positionRequestPubkey = new PublicKey(_positionRequestPubkey);
    const positionRequest =
      await JUPITER_PERPETUALS_PROGRAM.account.positionRequest.fetch(
        positionRequestPubkey,
      );

    // Setup transaction instructions
    const preInstructions: TransactionInstruction[] = [];
    const isSOL = positionRequest.mint.equals(NATIVE_MINT);

    const positionRequestAta = getAssociatedTokenAddressSync(
      positionRequest.mint,
      positionRequestPubkey,
      true,
    );

    const ownerATA = getAssociatedTokenAddressSync(
      positionRequest.mint,
      positionRequest.owner,
      true,
    );

    if (!isSOL) {
      const createOwnerATA = createAssociatedTokenAccountIdempotentInstruction(
        positionRequest.owner,
        ownerATA,
        positionRequest.owner,
        positionRequest.mint,
      );

      preInstructions.push(createOwnerATA);
    }

    const closePositionRequestIx = await JUPITER_PERPETUALS_PROGRAM.methods
      .closePositionRequest({})
      .accounts({
        keeper: null,
        owner: positionRequest.owner,
        ownerAta: isSOL ? null : ownerATA,
        pool: JLP_POOL_ACCOUNT_PUBKEY,
        positionRequest: positionRequestPubkey,
        positionRequestAta,
        position: positionRequest.position,
      })
      .instruction();

    // Construct transaction
    const instructions = [
      ComputeBudgetProgram.setComputeUnitPrice({
        microLamports: 100000, // Get the estimated compute unit price here from RPC or a provider like Triton
      }),
      ...preInstructions,
      closePositionRequestIx,
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
    const { blockhash, lastValidBlockHeight } =
      await RPC_CONNECTION.getLatestBlockhash("confirmed");

    const txMessage = new TransactionMessage({
      payerKey: positionRequest.owner,
      recentBlockhash: blockhash,
      instructions,
    }).compileToV0Message();

    const tx = new VersionedTransaction(txMessage);

    // Sign transaction
    const secretKey = Uint8Array.from(
      utils.bytes.bs58.decode("SECRET_KEY" as string),
    );
    const keypair = Keypair.fromSecretKey(secretKey);
    tx.sign([keypair]);

    // Submit transaction
    const txid = await RPC_CONNECTION.sendTransaction(tx);
    const confirmation = await RPC_CONNECTION.confirmTransaction(
      {
        blockhash,
        lastValidBlockHeight,
        signature: txid,
      },
      "confirmed",
    );
    console.log("transaction confirmation", confirmation);
    console.log(`https://solscan.io/tx/${txid}`);
  } catch (error) {}
}
