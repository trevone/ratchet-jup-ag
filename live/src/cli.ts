// CLI for the Python bot. Simulates by default; sends only when argument "send": true AND env JUP_LIVE=1.
// Market-specific commands take "market": "SOL" | "ETH" | "BTC" (default SOL = original behaviour).
import { BN } from "@coral-xyz/anchor";
import { PublicKey, SystemProgram } from "@solana/web3.js";
import { Built, buildCancelRequest, buildCloseEmptyAtas, buildUpdateTrigger, buildDecrease, buildIncrease, connect, market, ownerTokenAccounts, positionFlows, positionIncreases, programs, readState, simulate } from "./jup";
import { awaitRequestGone, loadKeypair, sendBuilt } from "./send";
import { buildCancelLimit, buildLimit, listLimitOrders, signAndSubmit, simulateApiTx } from "./limit";
import { buildSendJupsol, buildSwap, quote, readStake, readSweepTokens, simulateBuilt, txDeltas, SwapBuilt, SWEEP_MINTS } from "./stake";

const OWNER = new PublicKey(process.env.JUP_OWNER ?? "Csa1b6D2h7i63ZgrBULLLL76B9uSBmYdKyP47UFCgwx");

async function run(conn: any, b: Built, a: any, market: boolean, idempotent = true) {
  if (!(a.send && process.env.JUP_LIVE === "1")) return await simulate(conn, OWNER, b);
  const kp = loadKeypair(OWNER.toBase58());
  const r = await sendBuilt(conn, kp, b, idempotent);
  const out: any = { label: b.label, sent: true, ...r, positionRequest: b.positionRequest.toBase58() };
  if (r.ok && market) out.fulfillSec = await awaitRequestGone(conn, b.positionRequest);
  return out;
}

/** Stake-bucket txs: never re-signed at a higher price (a duplicate could execute twice), rebroadcast only. */
async function runStake(conn: any, b: SwapBuilt, a: any) {
  if (!(a.send && process.env.JUP_LIVE === "1")) return { ...b.info, ...(await simulateBuilt(conn, OWNER, b)) };
  const kp = loadKeypair(OWNER.toBase58());
  const r = await sendBuilt(conn, kp, b, false);
  return { ...b.info, label: b.label, sent: true, ...r, deltas: r.sig ? await txDeltas(conn, r.sig, OWNER) : null };
}

async function main() {
  const [cmd, argJson] = process.argv.slice(2);
  const a = argJson ? JSON.parse(argJson) : {};
  const conn = await connect();
  let out: unknown;
  if (cmd === "state") {
    const st: any = await readState(conn, OWNER, market(a.market));   // wallet.sol stays NATIVE lamports only (bot margin)
    st.stake = await readStake(conn, OWNER).catch(e => ({ error: String(e).slice(0, 160) }));   // separate stake bucket
    st.sweepTokens = await readSweepTokens(conn, OWNER).catch(e => ({ error: String(e).slice(0, 160) }));   // non-SOL leftovers
    out = st;
  } else if (cmd === "stake-state") out = await readStake(conn, OWNER);
  else if (cmd === "swap-quote") out = { ok: true, ...(await quote(a)).quote };
  else if (cmd === "simulate-swap" || cmd === "swap") {
    out = await runStake(conn, await buildSwap(conn, OWNER, a), a);
  } else if (cmd === "simulate-send-jupsol" || cmd === "send-jupsol") {
    out = await runStake(conn, await buildSendJupsol(conn, OWNER, a.to, a.amountAtoms), a);
  }
  else if (cmd === "simulate-increase" || cmd === "increase") {
    const minOut = a.jupiterMinimumOut ? new BN(a.jupiterMinimumOut) : null;
    // own-coin ledger (2026-10-08): ETH / BTC bots fund from their own coin (ETH / WBTC); SOL default unchanged
    const INPUTS: Record<string, string> = { SOL: "So11111111111111111111111111111111111111112", ...SWEEP_MINTS };
    if (a.inputMint !== undefined && !Object.values(INPUTS).includes(a.inputMint)) throw new Error(`input mint ${a.inputMint} not allowed`);
    const b = await buildIncrease(conn, OWNER, a.side, BigInt(a.collateralAtoms), a.sizeUsd, a.priceSlippageUsd,
                                  a.inputMint ? new PublicKey(a.inputMint) : undefined, minOut, market(a.market));
    out = await run(conn, b, a, true);
  } else if (cmd === "simulate-decrease" || cmd === "decrease") {
    out = await run(conn, await buildDecrease(conn, OWNER, a.side, a, market(a.market)), a, a.triggerPriceUsd === undefined);
  } else if (cmd === "simulate-cancel" || cmd === "cancel") {
    out = await run(conn, await buildCancelRequest(conn, OWNER, new PublicKey(a.request)), a, false);
  } else if (cmd === "simulate-update-trigger" || cmd === "update-trigger") {   // TP2_SET / TP3_SET: move a TP in place
    out = await run(conn, await buildUpdateTrigger(conn, OWNER, new PublicKey(a.request), a.triggerPriceUsd), a, false);
  } else if (cmd === "limit-orders") {   // read-only: this wallet's resting limit orders (Jupiter Perps API)
    out = await listLimitOrders(OWNER);
  } else if (cmd === "simulate-create-limit-order" || cmd === "create-limit-order") {
    // resting limit add ([resting] adds_enabled, default OFF): API builds, we verify, owner signs, /transaction/execute
    const b: any = await buildLimit(conn, OWNER, market(a.market), { side: a.side, inputMint: a.inputMint ?? "So11111111111111111111111111111111111111112",
      inputAtoms: String(a.inputAtoms), sizeUsd: a.sizeUsd, triggerPriceUsd: a.triggerPriceUsd });
    const { tx, ...info } = b;
    if (!b.ok || !tx) out = { ...info, ok: false };
    else if (!(a.send && process.env.JUP_LIVE === "1")) out = { ...info, ...(await simulateApiTx(conn, tx)), sent: false };
    else out = { ...info, ...(await signAndSubmit(conn, loadKeypair(OWNER.toBase58()), tx, "create-limit-order", b.requireKeeperSignature !== false)) };
  } else if (cmd === "simulate-cancel-limit-order" || cmd === "cancel-limit-order") {
    const b: any = await buildCancelLimit(conn, OWNER, market(a.market), a.request);
    const { tx, ...info } = b;
    if (!b.ok || !tx) out = { ...info, ok: false };
    else if (!(a.send && process.env.JUP_LIVE === "1")) out = { ...info, ...(await simulateApiTx(conn, tx)), sent: false };
    else out = { ...info, ...(await signAndSubmit(conn, loadKeypair(OWNER.toBase58()), tx, "cancel-limit-order", b.requireKeeperSignature !== false)) };
  } else if (cmd === "position-increases") {   // read-only: recent increase (add) fills of this position
    out = await positionIncreases(conn, new PublicKey(a.position), Number(a.limit ?? 40));
  } else if (cmd === "position-flows") {   // read-only: this position's own SOL flows since its open tx
    out = await positionFlows(conn, OWNER, new PublicKey(a.position), a.sinceSig);
  } else if (cmd === "token-accounts") {   // read-only: the owner's bot token accounts (rent deposits = owner capital)
    const atas = ownerTokenAccounts(OWNER);
    const names = Object.keys(atas);
    const accs = await conn.getMultipleAccountsInfo(names.map(n => new PublicKey(atas[n])));
    out = { ok: true, owner: OWNER.toBase58(), rentExemptLamports: await conn.getMinimumBalanceForRentExemption(165),
      accounts: Object.fromEntries(names.map((n, i) => [n, { account: atas[n], exists: !!accs[i], lamports: accs[i]?.lamports ?? 0,
        atoms: accs[i] && accs[i]!.data.length >= 72 ? accs[i]!.data.readBigUInt64LE(64).toString() : "0" }])) };
  } else if (cmd === "simulate-close-empty-atas" || cmd === "close-empty-atas") {   // rent reclaim (only when all bots are flat)
    const b = await buildCloseEmptyAtas(conn, OWNER, a.names ?? []);
    if (!b.instructions.length) out = { ok: true, nothing: true, ...b.info };
    else out = { ...b.info, ...(await run(conn, b, a, false, false)) };
  } else if (cmd === "transfer") {
    const ALLOWED = "59su4N4uaKcN8YZtYYgBj9kdUGytJgAF6HBTiyxzjGzp";   // only allowed destination
    if (a.to !== ALLOWED) throw new Error("transfer destination not allowed");
    const to = new PublicKey(ALLOWED);
    const b: Built = { label: `transfer ${a.lamports} lamports`, positionRequest: to,
      instructions: [SystemProgram.transfer({ fromPubkey: OWNER, toPubkey: to, lamports: BigInt(a.lamports) })] };
    out = await run(conn, b, a, false, false);   // transfers never escalate (a re-sign could pay twice)
  } else throw new Error(`unknown command ${cmd}`);
  console.log(JSON.stringify(out));
}
main().catch(e => { console.log(JSON.stringify({ ok: false, error: String(e).slice(0, 400) })); process.exit(1); });
