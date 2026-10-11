// Jupiter Perps (SOL / ETH / BTC markets) read-only state + request builders + simulation. Never sends transactions.
// Every market-specific call takes `market` (default "SOL" = the original single-market behaviour).
import { AnchorProvider, BN, Program, Wallet } from "@coral-xyz/anchor";
import {
  ComputeBudgetProgram, Connection, Keypair, PublicKey, SystemProgram,
  TransactionInstruction, TransactionMessage, VersionedTransaction,
} from "@solana/web3.js";
import {
  NATIVE_MINT, createAssociatedTokenAccountIdempotentInstruction, createCloseAccountInstruction,
  createSyncNativeInstruction, getAssociatedTokenAddressSync,
} from "@solana/spl-token";
import { IDL, type Perpetuals } from "./idl/jupiter-perpetuals-idl";
import { IDL as DOVES_IDL, type Doves } from "./idl/doves-idl";

export const PERPS_PROGRAM_ID = new PublicKey("PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu");
export const DOVES_PROGRAM_ID = new PublicKey("DoVEsk76QybCEHQGzkvYPWLQu9gzNoZZZt3TPiL597e");
export const POOL = new PublicKey("5BUwFW4nRbftYTDMbgxykoFWqWHPzahFSNAaaaJtVKsq");
export const SOL_CUSTODY = new PublicKey("7xS2gz2bTp3fwCC7knJvUWTEU9Tycczu6VhJYKgi1wdz");
export const USDC_CUSTODY = new PublicKey("G18jKKXQwBbrHeiK3C9MRXhkHsLHf7XgCSisykV46EZa");
export const USDC_MINT = new PublicKey("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v");   // = USDC custody mint (verified on-chain)
export const ETH_CUSTODY = new PublicKey("AQCGyheWPLeo6Qp9WpYS9m3Qj479t7R636N9ey1rEjEn");   // mint 7vfCXT.. (Wormhole ETH), 8 dec
export const BTC_CUSTODY = new PublicKey("5Pv3gM9JrFFH883SWAhvJC9RPYmo8UNxuFtv5bMMALkm");   // mint 3NZ9JM.. (WBTC), 8 dec

/** Perps markets traded by the bots. custody = market custody (also the long collateral custody); verified 2026-10-07
 * from pool 5BUwFW.. custodies: mint / decimals / dovesAgOracle are re-read from the custody account at runtime. */
export type Market = "SOL" | "ETH" | "BTC";
export const MARKETS: Record<Market, { custody: PublicKey; mint: PublicKey; decimals: number }> = {
  SOL: { custody: SOL_CUSTODY, mint: NATIVE_MINT, decimals: 9 },
  ETH: { custody: ETH_CUSTODY, mint: new PublicKey("7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs"), decimals: 8 },
  BTC: { custody: BTC_CUSTODY, mint: new PublicKey("3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh"), decimals: 8 },
};
export function market(m: unknown): Market {
  const k = String(m ?? "SOL").toUpperCase();
  if (!(k in MARKETS)) throw new Error(`unknown market ${k}`);
  return k as Market;
}
const PERPETUALS = PublicKey.findProgramAddressSync([Buffer.from("perpetuals")], PERPS_PROGRAM_ID)[0];
export const USD = 1e6;            // USD amounts are 6-decimal fixed point
const CU_PRICE_MICROLAMPORTS = Number(process.env.CU_PRICE_MICROLAMPORTS ?? 100_000);
const RPC_TIMEOUT_MS = 8_000;

export type Side = "long" | "short";

function withTimeout(ms: number): typeof fetch {
  return (input, init) => fetch(input, { ...init, signal: AbortSignal.timeout(ms) });
}

/** Helius first, public RPC as fallback. The URL (with the API key) is never logged. */
export async function connect(): Promise<Connection> {
  const urls = [
    process.env.HELIUS_API_KEY ? `https://mainnet.helius-rpc.com/?api-key=${process.env.HELIUS_API_KEY}` : null,
    "https://api.mainnet-beta.solana.com",
  ].filter((u): u is string => !!u);
  for (const [i, url] of urls.entries()) {
    const c = new Connection(url, { commitment: "confirmed", fetch: withTimeout(RPC_TIMEOUT_MS) });
    try { await c.getSlot(); return c; } catch { console.error(`rpc #${i} unavailable, trying next`); }
  }
  throw new Error("no RPC reachable");
}

export function programs(conn: Connection) {
  const provider = new AnchorProvider(conn, new Wallet(Keypair.generate()), AnchorProvider.defaultOptions());
  return { perps: new Program<Perpetuals>(IDL, PERPS_PROGRAM_ID, provider), doves: new Program<Doves>(DOVES_IDL, DOVES_PROGRAM_ID, provider) };
}

export function positionPda(owner: PublicKey, side: Side, mkt: Market = "SOL"): PublicKey {
  const custody = MARKETS[mkt].custody;
  const collateral = side === "long" ? custody : USDC_CUSTODY;
  return PublicKey.findProgramAddressSync(
    [Buffer.from("position"), owner.toBuffer(), POOL.toBuffer(), custody.toBuffer(), collateral.toBuffer(),
     Buffer.from([side === "long" ? 1 : 2])], PERPS_PROGRAM_ID)[0];
}

export function positionRequestPda(position: PublicKey, change: "increase" | "decrease", counter: BN): PublicKey {
  return PublicKey.findProgramAddressSync(
    [Buffer.from("position_request"), position.toBuffer(), counter.toArrayLike(Buffer, "le", 8),
     Buffer.from([change === "increase" ? 1 : 2])], PERPS_PROGRAM_ID)[0];
}

const newCounter = () => new BN(Math.floor(Math.random() * 1_000_000_000));
const usd = (x: number) => new BN(Math.round(x * USD));

// ------------------------------------------------------------------ read-only state
async function readOracle(doves: any, custody: any) {
  try {
    // dovesAgOracle is the aggregated feed the keepers price against; plain dovesOracle can be stale
    const acct = custody.dovesAgOracle ?? custody.dovesOracle;
    const feed: any = await doves.account.agPriceFeed?.fetch(acct).catch(() => null) ?? await doves.account.priceFeed.fetch(acct);
    const ts = Number(feed.timestamp.toString());
    return { price: Number(feed.price.toString()) * 10 ** feed.expo, ts, ageSec: Math.round(Date.now() / 1000 - ts), account: acct.toBase58() };
  } catch (e) { return { error: String(e).slice(0, 160) }; }
}

/** State for one market. Shared-wallet fields: ``openRequests`` = this market's requests only (both sides);
 * ``allOpenRequests`` = every request of the owner; ``allPositions`` = collateral of every open position (all markets);
 * ``solOracle`` = SOL price (margin is native SOL, so every bot converts USD <-> SOL with it). */
export async function readState(conn: Connection, owner: PublicKey, mkt: Market = "SOL") {
  const { perps, doves } = programs(conn);
  const custody: any = await perps.account.custody.fetch(MARKETS[mkt].custody);
  if (!custody.mint.equals(MARKETS[mkt].mint)) throw new Error(`custody mint mismatch for ${mkt}`);
  const pool: any = await perps.account.pool.fetch(POOL);
  const oracle: any = await readOracle(doves, custody);
  const solOracle: any = mkt === "SOL" ? oracle : await readOracle(doves, await perps.account.custody.fetch(SOL_CUSTODY));
  const positions: Record<string, unknown> = {};
  for (const side of ["long", "short"] as Side[]) {
    const pda = positionPda(owner, side, mkt);
    const p: any = await perps.account.position.fetchNullable(pda);
    positions[side] = p && !p.sizeUsd.isZero()
      ? { pubkey: pda.toBase58(), sizeUsd: p.sizeUsd.toNumber() / USD, collateralUsd: p.collateralUsd.toNumber() / USD,
          price: p.price.toNumber() / USD, openTime: p.openTime.toNumber(), realisedPnlUsd: p.realisedPnlUsd.toNumber() / USD }
      : { pubkey: pda.toBase58(), open: false, realisedPnlUsd: p ? p.realisedPnlUsd.toNumber() / USD : null };
  }
  const reqs = await perps.account.positionRequest.all([{ memcmp: { offset: 8, bytes: owner.toBase58() } }]);
  const lamports = await conn.getBalance(owner);
  // every position of the owner across markets (budget split: total = native SOL + all open collateral)
  const keys: { market: Market; side: Side; pda: PublicKey }[] = [];
  for (const m of Object.keys(MARKETS) as Market[]) for (const side of ["long", "short"] as Side[]) keys.push({ market: m, side, pda: positionPda(owner, side, m) });
  const accs: any[] = await perps.account.position.fetchMultiple(keys.map(k => k.pda));
  const allPositions = keys.map((k, i) => ({ market: k.market, side: k.side, pubkey: k.pda.toBase58(),
    sizeUsd: accs[i] ? accs[i].sizeUsd.toNumber() / USD : 0, collateralUsd: accs[i] && !accs[i].sizeUsd.isZero() ? accs[i].collateralUsd.toNumber() / USD : 0 }))
    .filter(p => p.sizeUsd > 0);
  const mine = new Set((["long", "short"] as Side[]).map(sd => positionPda(owner, sd, mkt).toBase58()));
  const fmt = (r: any) => ({ pubkey: r.publicKey.toBase58(), position: r.account.position.toBase58(),
      requestType: Object.keys(r.account.requestType)[0],
      requestChange: Object.keys(r.account.requestChange)[0], triggerPrice: r.account.triggerPrice?.toNumber?.() / USD,
      executed: r.account.executed });
  return {
    market: mkt,
    wallet: { owner: owner.toBase58(), sol: lamports / 1e9 },
    positions,
    oracle,
    solOracle,
    allPositions,
    custody: {
      hourlyBorrowRate: custody.fundingRateState.hourlyFundingDbps.toNumber() / 1e5 / 100,
      utilization: custody.assets.locked.toNumber() / custody.assets.owned.toNumber(),
      jumpRate: custody.jumpRateState ? Object.fromEntries(Object.entries(custody.jumpRateState).map(([k, v]: any) => [k, v.toString()])) : null,
      maxLeverage: custody.pricing.maxLeverage.toNumber() / 1e4,
      increasePositionBps: custody.increasePositionBps?.toNumber?.(),
      decreasePositionBps: custody.decreasePositionBps?.toNumber?.(),
      assets: { owned: custody.assets.owned.toString(), locked: custody.assets.locked.toString() },
    },
    pool: { name: pool.name, maxRequestExecutionSec: pool.maxRequestExecutionSec?.toString?.() },
    openRequests: reqs.filter((r: any) => mine.has(r.account.position.toBase58())).map(fmt),   // this market only
    allOpenRequests: reqs.map(fmt),
  };
}

// ------------------------------------------------------------------ builders
export interface Built { label: string; instructions: TransactionInstruction[]; positionRequest: PublicKey }

/** Open/increase. Long: SOL collateral (wrapped from native). Short: USDC collateral; SOL input is swapped
 * by the keeper (jupiterMinimumOut required, from a Jupiter quote). */
export async function buildIncrease(conn: Connection, owner: PublicKey, side: Side, collateralLamportsOrUsdcAtoms: bigint,
                                    sizeUsd: number, priceSlippageUsd: number, inputMint = NATIVE_MINT,
                                    jupiterMinimumOut: BN | null = null, mkt: Market = "SOL"): Promise<Built> {
  const { perps } = programs(conn);
  const custody = MARKETS[mkt].custody;
  const collateralCustody = side === "long" ? custody : USDC_CUSTODY;
  const collateralMint = side === "long" ? MARKETS[mkt].mint : USDC_MINT;
  // input != collateral token -> the keeper swaps it at execution; the program needs jupiterMinimumOut for that
  if (!inputMint.equals(collateralMint) && (jupiterMinimumOut === null || jupiterMinimumOut.lten(0)))
    throw new Error(`${mkt} ${side}: input ${inputMint.toBase58()} != collateral ${collateralMint.toBase58()} needs jupiterMinimumOut`);
  const position = positionPda(owner, side, mkt);
  const counter = newCounter();
  const positionRequest = positionRequestPda(position, "increase", counter);
  const fundingAccount = getAssociatedTokenAddressSync(inputMint, owner);
  const pre: TransactionInstruction[] = [], post: TransactionInstruction[] = [];
  if (inputMint.equals(NATIVE_MINT)) {
    pre.push(createAssociatedTokenAccountIdempotentInstruction(owner, fundingAccount, owner, NATIVE_MINT),
             SystemProgram.transfer({ fromPubkey: owner, toPubkey: fundingAccount, lamports: collateralLamportsOrUsdcAtoms }),
             createSyncNativeInstruction(fundingAccount));
    post.push(createCloseAccountInstruction(fundingAccount, owner, owner));
  }
  const ix = await perps.methods.createIncreasePositionMarketRequest({
    counter, collateralTokenDelta: new BN(collateralLamportsOrUsdcAtoms.toString()), jupiterMinimumOut,
    priceSlippage: usd(priceSlippageUsd), side: side === "long" ? { long: {} } : { short: {} }, sizeUsdDelta: usd(sizeUsd),
  } as any).accounts({
    custody, collateralCustody, fundingAccount, inputMint,
    owner, perpetuals: PERPETUALS, pool: POOL, position, positionRequest,
    positionRequestAta: getAssociatedTokenAddressSync(inputMint, positionRequest, true), referral: null,
  } as any).instruction();
  return { label: `increase ${mkt} ${side} size $${sizeUsd}`, instructions: [...pre, ix, ...post], positionRequest };
}

/** Market decrease (partial: sizeUsd > 0; full: entire=true), or trigger (TP/SL) when triggerPriceUsd is set. */
export async function buildDecrease(conn: Connection, owner: PublicKey, side: Side, opts: {
  sizeUsd?: number; entire?: boolean; priceSlippageUsd?: number; triggerPriceUsd?: number; triggerAbove?: boolean;
  desiredMint?: PublicKey; }, mkt: Market = "SOL"): Promise<Built> {
  const { perps } = programs(conn);
  const marketCustody = MARKETS[mkt].custody;
  const position = positionPda(owner, side, mkt);
  const counter = newCounter();
  const positionRequest = positionRequestPda(position, "decrease", counter);
  const desiredMint = opts.desiredMint ?? NATIVE_MINT;   // proceeds as SOL (keeper swaps USDC / ETH / WBTC collateral -> SOL)
  const receivingAccount = getAssociatedTokenAddressSync(desiredMint, owner, true);
  const custody: any = await perps.account.custody.fetch(marketCustody);
  const collateralCustody = side === "long" ? marketCustody : USDC_CUSTODY;
  const trigger = opts.triggerPriceUsd !== undefined;
  const ix = await perps.methods.createDecreasePositionRequest2({
    collateralUsdDelta: new BN(0), sizeUsdDelta: usd(opts.entire ? 0 : opts.sizeUsd ?? 0),
    requestType: trigger ? { trigger: {} } : { market: {} },
    priceSlippage: trigger ? null : usd(opts.priceSlippageUsd ?? 0), jupiterMinimumOut: null,
    triggerPrice: trigger ? usd(opts.triggerPriceUsd!) : null, triggerAboveThreshold: trigger ? !!opts.triggerAbove : null,
    entirePosition: opts.entire ?? false, counter,
  } as any).accounts({
    owner, receivingAccount, perpetuals: PERPETUALS, pool: POOL, position, positionRequest,
    positionRequestAta: getAssociatedTokenAddressSync(desiredMint, positionRequest, true), custody: marketCustody,
    custodyDovesPriceAccount: custody.dovesAgOracle, custodyPythnetPriceAccount: custody.oracle.oracleAccount,
    collateralCustody, desiredMint, referral: null,
  } as any).instruction();
  const pre = [createAssociatedTokenAccountIdempotentInstruction(owner, receivingAccount, owner, desiredMint)];
  const kind = trigger ? `trigger@${opts.triggerPriceUsd} (${opts.triggerAbove ? "above" : "below"})` : "market";
  return { label: `decrease ${mkt} ${side} ${opts.entire ? "entire" : `$${opts.sizeUsd}`} ${kind}`, instructions: [...pre, ix], positionRequest };
}

/** Cancel a pending request (e.g. a stale TP after the average entry moved); replace = cancel + new trigger. */
export async function buildCancelRequest(conn: Connection, owner: PublicKey, request: PublicKey): Promise<Built> {
  const { perps } = programs(conn);
  const r: any = await perps.account.positionRequest.fetch(request);
  // v1 `closePositionRequest` is gone on mainnet (InstructionFallbackNotFound); use closePositionRequest2.
  const ownerAta = getAssociatedTokenAddressSync(r.mint, owner, true);
  const ix = await perps.methods.closePositionRequest2().accounts({
    keeper: null, owner, ownerAta, pool: POOL, positionRequest: request,
    positionRequestAta: getAssociatedTokenAddressSync(r.mint, request, true), position: r.position, mint: r.mint,
  } as any).instruction();
  const pre = [createAssociatedTokenAccountIdempotentInstruction(owner, ownerAta, owner, r.mint)];
  return { label: `cancel request ${request.toBase58()}`, instructions: [...pre, ix], positionRequest: request };
}

// ------------------------------------------------------------------ per-position SOL flows (shared-wallet ledger)
/** Owner SOL flow of every tx that touched ``position`` from the slot of ``sinceSig`` (inclusive) on: native lamports of
 * the owner + lamports of the owner's wSOL ATA (rent / wrapped proceeds), post - pre. Opens, adds, TP/cancel requests
 * (fees + request rent) and the keepers' executions (proceeds, rent refunds) all touch the position; deposits, stake
 * swaps and other markets' trades never do, so this is the bot's own realized flow on a shared wallet. Read-only. */
export async function positionFlows(conn: Connection, owner: PublicKey, position: PublicKey, sinceSig: string) {
  const first = await conn.getTransaction(sinceSig, { maxSupportedTransactionVersion: 0, commitment: "confirmed" });
  if (!first) throw new Error("sinceSig not found");
  const minSlot = first.slot;
  const sigs: { signature: string; slot: number }[] = [];
  let before: string | undefined;
  for (let page = 0; page < 10; page++) {
    const batch = await conn.getSignaturesForAddress(position, { before, limit: 1000 }, "confirmed");
    for (const b of batch) if (b.slot >= minSlot) sigs.push({ signature: b.signature, slot: b.slot });
    if (batch.length < 1000 || batch[batch.length - 1].slot < minSlot) break;
    before = batch[batch.length - 1].signature;
  }
  if (!sigs.some(x => x.signature === sinceSig)) sigs.push({ signature: sinceSig, slot: minSlot });
  const wsol = getAssociatedTokenAddressSync(NATIVE_MINT, owner).toBase58();
  let net = 0, fees = 0, missing = 0;
  const rows: any[] = [];
  for (const { signature } of sigs) {
    let t: any = null;
    for (let k = 0; k < 4 && !t; k++) {
      t = await conn.getTransaction(signature, { maxSupportedTransactionVersion: 0, commitment: "confirmed" }).catch(() => null);
      if (!t) await new Promise(r => setTimeout(r, 800));
    }
    if (!t?.meta) { missing++; continue; }
    const keys = [...t.transaction.message.staticAccountKeys.map((k: PublicKey) => k.toBase58()),
                  ...(t.meta.loadedAddresses?.writable ?? []).map((k: any) => k.toString()),
                  ...(t.meta.loadedAddresses?.readonly ?? []).map((k: any) => k.toString())];
    let d = 0;
    for (const who of [owner.toBase58(), wsol]) {
      const i = keys.indexOf(who);
      if (i >= 0) d += t.meta.postBalances[i] - t.meta.preBalances[i];
    }
    const payer = keys[0] === owner.toBase58();
    if (payer) fees += t.meta.fee;
    net += d;
    rows.push({ sig: signature, slot: t.slot, blockTime: t.blockTime, deltaLamports: d, ownerPaid: payer, err: t.meta.err ? true : false });
  }
  return { ok: missing === 0, position: position.toBase58(), sinceSig, netLamports: net, netSol: net / 1e9,
           ownerFeeLamports: fees, nTx: rows.length, missing, txs: rows.sort((a, b) => a.slot - b.slot) };
}

// ------------------------------------------------------------------ simulate (NEVER send)
export async function simulate(conn: Connection, payer: PublicKey, b: Built) {
  const ixs = [ComputeBudgetProgram.setComputeUnitPrice({ microLamports: CU_PRICE_MICROLAMPORTS }), ...b.instructions];
  const msg = new TransactionMessage({ payerKey: payer, recentBlockhash: PublicKey.default.toBase58(), instructions: ixs }).compileToV0Message();
  const tx = new VersionedTransaction(msg);
  const sim = await conn.simulateTransaction(tx, { replaceRecentBlockhash: true, sigVerify: false });
  const logs = sim.value.logs ?? [];
  return {
    label: b.label, ok: sim.value.err === null, err: sim.value.err, unitsConsumed: sim.value.unitsConsumed,
    txBytes: tx.serialize().length, positionRequest: b.positionRequest.toBase58(),
    logs: logs.filter(l => /Instruction:|Error|error|failed|success|consumed/.test(l)).slice(0, 14),
  };
}
