// ONE-OFF approved $20 test sequence on Jupiter Perps (SOL long, SOL collateral). Explicit send path.
// Not used by the strategy loop (which stays dry_run). The secret key is read in-process only, never logged.
import fs from "fs";
import { BN } from "@coral-xyz/anchor";
import { ComputeBudgetProgram, Connection, Keypair, PublicKey, TransactionMessage, VersionedTransaction } from "@solana/web3.js";
import { NATIVE_MINT, createCloseAccountInstruction, getAssociatedTokenAddressSync } from "@solana/spl-token";
import { Built, USD, buildCancelRequest, buildDecrease, buildIncrease, connect, positionPda, programs, readState } from "./jup";

const KEY_PATH = "/home/box/.secrets/jup-trader.json";
const EXPECTED_OWNER = "Csa1b6D2h7i63ZgrBULLLL76B9uSBmYdKyP47UFCgwx";
const CAP = { collateralUsd: 10.5, collateralSol: 0.09, sizeUsd: 21, leverage: 2.1 };
const COLLATERAL_USD = 10, SIZE_USD = 20, SLIPPAGE = 0.01, MAX_ORACLE_AGE = 60;
const FULFILL_TIMEOUT_MS = 90_000, POLL_MS = 2_000, CU_PRICE = 1_000_000;
const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));
const log = (...a: unknown[]) => console.log(new Date().toISOString(), ...a);
const txs: { step: string; sig: string; ok: boolean; note?: string }[] = [];

const kp = Keypair.fromSecretKey(Uint8Array.from(JSON.parse(fs.readFileSync(KEY_PATH, "utf8"))));
if (kp.publicKey.toBase58() !== EXPECTED_OWNER) throw new Error("keypair does not match expected owner");
const OWNER = kp.publicKey;
const POS = positionPda(OWNER, "long");

async function send(conn: Connection, step: string, b: Built): Promise<string> {
  const base = [ComputeBudgetProgram.setComputeUnitPrice({ microLamports: CU_PRICE }), ...b.instructions];
  const sim = await conn.simulateTransaction(new VersionedTransaction(new TransactionMessage({
    payerKey: OWNER, recentBlockhash: PublicKey.default.toBase58(), instructions: base }).compileToV0Message()),
    { replaceRecentBlockhash: true, sigVerify: false });
  if (sim.value.err) throw new Error(`${step}: simulation failed ${JSON.stringify(sim.value.err)} ${(sim.value.logs ?? []).slice(-4).join(" | ")}`);
  const units = Math.ceil((sim.value.unitsConsumed ?? 200_000) * 1.2);
  const { blockhash, lastValidBlockHeight } = await conn.getLatestBlockhash("confirmed");
  const tx = new VersionedTransaction(new TransactionMessage({ payerKey: OWNER, recentBlockhash: blockhash,
    instructions: [ComputeBudgetProgram.setComputeUnitLimit({ units }), ...base] }).compileToV0Message());
  tx.sign([kp]);
  const raw = tx.serialize();
  const sig = await conn.sendRawTransaction(raw, { skipPreflight: false, maxRetries: 0 });
  // rebroadcast until landed or the blockhash expires (single sends were being dropped)
  let err: unknown = undefined;
  while (err === undefined) {
    await sleep(1500);
    const st = (await conn.getSignatureStatuses([sig])).value[0];
    if (st && (st.confirmationStatus === "confirmed" || st.confirmationStatus === "finalized")) { err = st.err; break; }
    if ((await conn.getBlockHeight("confirmed")) > lastValidBlockHeight) {
      const fin = (await conn.getSignatureStatuses([sig], { searchTransactionHistory: true })).value[0];
      if (fin) { err = fin.err; break; }
      txs.push({ step, sig, ok: false, note: "expired, not landed" });
      throw new Error(`${step}: expired, not landed (${sig})`);
    }
    await conn.sendRawTransaction(raw, { skipPreflight: true, maxRetries: 0 }).catch(() => {});
  }
  const ok = err === null;
  txs.push({ step, sig, ok, note: ok ? undefined : JSON.stringify(err) });
  log(`[${step}] ${ok ? "confirmed" : "FAILED"} https://solscan.io/tx/${sig} (CU limit ${units})`);
  if (!ok) throw new Error(`${step}: tx failed`);
  return sig;
}

async function position(conn: Connection) {
  const { perps } = programs(conn);
  const p: any = await perps.account.position.fetchNullable(POS);
  return p && !p.sizeUsd.isZero() ? { sizeUsd: p.sizeUsd.toNumber() / USD, collateralUsd: p.collateralUsd.toNumber() / USD,
    price: p.price.toNumber() / USD } : null;
}

async function requests(conn: Connection) {
  const { perps } = programs(conn);
  const all = await perps.account.positionRequest.all([{ memcmp: { offset: 8, bytes: OWNER.toBase58() } }]);
  return all.map((r: any) => ({ pubkey: r.publicKey, type: Object.keys(r.account.requestType)[0],
    trigger: r.account.triggerPrice ? r.account.triggerPrice.toNumber() / USD : null,
    sizeUsd: r.account.sizeUsdDelta.toNumber() / USD, entire: r.account.entirePosition, executed: r.account.executed }));
}

/** Wait until the market request account is gone (executed or closed by keeper). */
async function awaitFulfillment(conn: Connection, step: string, req: PublicKey, before: any) {
  const t0 = Date.now();
  while (Date.now() - t0 < FULFILL_TIMEOUT_MS) {
    await sleep(POLL_MS);
    const info = await conn.getAccountInfo(req);
    if (!info) {
      const after = await position(conn);
      const secs = (Date.now() - t0) / 1000;
      const changed = JSON.stringify(after) !== JSON.stringify(before);
      log(`[${step}] request closed after ~${secs.toFixed(1)}s; position changed=${changed}`, after);
      if (!changed) throw new Error(`${step}: request closed but position unchanged (keeper rejected?)`);
      return { secs, after };
    }
  }
  throw new Error(`${step}: keeper fulfillment timeout`);
}

async function oraclePrice(conn: Connection): Promise<number> {
  const s: any = await readState(conn, OWNER);
  if (!s.oracle?.price || s.oracle.ageSec > MAX_ORACLE_AGE) throw new Error(`oracle stale/unavailable ${JSON.stringify(s.oracle)}`);
  return s.oracle.price;
}

async function cancelAll(conn: Connection, step: string) {
  for (const r of await requests(conn)) await send(conn, step, await buildCancelRequest(conn, OWNER, r.pubkey));
  const left = await requests(conn);
  if (left.length) throw new Error(`${step}: ${left.length} requests still open`);
  log(`[${step}] no open requests`);
}

async function placeTp(conn: Connection, step: string, price: number, opts: { sizeUsd?: number; entire?: boolean }) {
  await send(conn, step, await buildDecrease(conn, OWNER, "long", { ...opts, triggerPriceUsd: Math.round(price * 100) / 100, triggerAbove: true }));
}

async function main() {
  const conn = await connect();
  const startLamports = await conn.getBalance(OWNER);
  const results: Record<string, unknown> = { startSol: startLamports / 1e9 };
  try {
    // 1. pre-check
    const px = await oraclePrice(conn);
    const RESUME = process.env.RESUME === "1";   // continue the same approved test on its own open position
    const existing = await position(conn);
    if (existing && !(RESUME && existing.sizeUsd <= CAP.sizeUsd && existing.sizeUsd / existing.collateralUsd <= CAP.leverage))
      throw new Error("existing long position: abort");
    if (RESUME && !existing) throw new Error("RESUME set but no position");
    if (!RESUME && (await requests(conn)).length) throw new Error("existing requests: abort");
    const collSol = COLLATERAL_USD / px;
    if (COLLATERAL_USD > CAP.collateralUsd || collSol > CAP.collateralSol || SIZE_USD > CAP.sizeUsd ||
        SIZE_USD / COLLATERAL_USD > CAP.leverage) throw new Error("cap exceeded: abort");
    log(`[precheck] ok: ${startLamports / 1e9} SOL, oracle ${px}, collateral ${collSol.toFixed(6)} SOL`);
    results.startPrice = px;

    let entry: number;
    if (RESUME) { entry = existing!.price; results.open = { ...existing, resumed: true }; log("[2] resumed", existing); }
    else {
    // 2. open long
    const open = await buildIncrease(conn, OWNER, "long", BigInt(Math.floor(collSol * 1e9)), SIZE_USD,
                                     Math.round(px * (1 + SLIPPAGE) * 100) / 100);
    await send(conn, "2 open long", open);
    const f1 = await awaitFulfillment(conn, "2 open long", open.positionRequest, null);
    entry = f1.after!.price;
    results.open = { ...f1.after, fulfillSec: f1.secs };
    }

    // 3. TP ladder, cancel, re-place (RESUME: the two TPs from run 2 already exist)
    if (!RESUME) {
    await placeTp(conn, "3a TP1 +$1.00 50%", entry + 1.0, { sizeUsd: SIZE_USD / 2 });
    await placeTp(conn, "3a TP2 +$1.50 rest", entry + 1.5, { entire: true });
    }
    results.tpPlaced = (await requests(conn)).map(r => ({ ...r, pubkey: r.pubkey.toBase58() }));
    log("[3a] requests", results.tpPlaced);
    await cancelAll(conn, "3b cancel TPs");
    await placeTp(conn, "3c TP1 re-place", entry + 1.0, { sizeUsd: SIZE_USD / 2 });
    await placeTp(conn, "3c TP2 re-place", entry + 1.5, { entire: true });
    results.tpReplaced = (await requests(conn)).map(r => ({ ...r, pubkey: r.pubkey.toBase58() }));
    log("[3c] requests", results.tpReplaced);

    // 4. cancel TPs, partial market close $10, single TP for the rest, cancel it
    await cancelAll(conn, "4a cancel TPs");
    const px2 = await oraclePrice(conn);
    const before = await position(conn);
    const part = await buildDecrease(conn, OWNER, "long", { sizeUsd: SIZE_USD / 2, priceSlippageUsd: Math.round(px2 * (1 - SLIPPAGE) * 100) / 100 });
    await send(conn, "4b partial close $10", part);
    const f2 = await awaitFulfillment(conn, "4b partial close $10", part.positionRequest, before);
    results.partial = { ...f2.after, fulfillSec: f2.secs, oracleAtRequest: px2 };
    await placeTp(conn, "4c TP rest +$1.50", f2.after!.price + 1.5, { entire: true });
    results.tpResized = (await requests(conn)).map(r => ({ ...r, pubkey: r.pubkey.toBase58() }));
    await cancelAll(conn, "4d cancel TP");
  } catch (e) {
    log("ERROR:", String(e));
    results.error = String(e);
  } finally {
    // 5. always try to leave nothing open: cancel requests, then close the position
    try {
      if ((await requests(conn)).length) await cancelAll(conn, "5a cleanup cancel");
      const pos = await position(conn);
      if (pos) {
        const px3 = await oraclePrice(conn);
        const full = await buildDecrease(conn, OWNER, "long", { entire: true, priceSlippageUsd: Math.round(px3 * (1 - SLIPPAGE) * 100) / 100 });
        await send(conn, "5b full close", full);
        const t0 = Date.now();
        while (Date.now() - t0 < FULFILL_TIMEOUT_MS && await conn.getAccountInfo(full.positionRequest)) await sleep(POLL_MS);
        results.full = { fulfillSec: (Date.now() - t0) / 1000, oracleAtRequest: px3, positionAfter: await position(conn) };
      }
      const wsol = getAssociatedTokenAddressSync(NATIVE_MINT, OWNER);
      if (await conn.getAccountInfo(wsol)) {   // unwrap SOL proceeds + reclaim ATA rent
        await send(conn, "5c unwrap wSOL", { label: "close wSOL ATA", instructions: [createCloseAccountInstruction(wsol, OWNER, OWNER)], positionRequest: wsol });
      }
    } catch (e) { log("CLEANUP ERROR:", String(e)); results.cleanupError = String(e); }
    const endLamports = await conn.getBalance(OWNER);
    results.final = { endSol: endLamports / 1e9, costSol: (startLamports - endLamports) / 1e9,
                      position: await position(conn), requests: (await requests(conn)).length,
                      wsolAta: !!(await conn.getAccountInfo(getAssociatedTokenAddressSync(NATIVE_MINT, OWNER))) };
    console.log("RESULT " + JSON.stringify({ txs, ...results }));
  }
}
main();
