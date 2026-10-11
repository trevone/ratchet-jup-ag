// Signing + sending (live only). Key loaded in-process from the keypair file; never logged.
import fs from "fs";
import { AddressLookupTableAccount, ComputeBudgetProgram, Connection, Keypair, PublicKey, TransactionMessage, VersionedTransaction } from "@solana/web3.js";
import { Built } from "./jup";

const KEY_PATH = process.env.JUP_KEY_PATH ?? "/home/box/.secrets/jup-trader.json";
const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));

export function loadKeypair(expected: string): Keypair {
  const kp = Keypair.fromSecretKey(Uint8Array.from(JSON.parse(fs.readFileSync(KEY_PATH, "utf8"))));
  if (kp.publicKey.toBase58() !== expected) throw new Error("keypair does not match expected owner");
  return kp;
}

const CU_FLOOR = Number(process.env.CU_PRICE_FLOOR ?? 10_000);      // micro-lamports per CU
const CU_CAP = Number(process.env.CU_PRICE_CAP ?? 200_000);
const ESCALATE_AFTER_MS = 5_000;
const ESCALATE_FACTOR = 3;
const CU_MARGIN = 1.1;
const isLanded = (st: { confirmationStatus?: string | null } | null): boolean =>
  !!st && (st.confirmationStatus === "confirmed" || st.confirmationStatus === "finalized");

/** Medium priority-fee estimate (Helius getPriorityFeeEstimate; fallback: median recent fees), clamped. */
export async function priorityFee(conn: Connection, accounts: PublicKey[]): Promise<number> {
  let est: number | null = null;
  try {
    if (conn.rpcEndpoint.includes("helius")) {
      const r = await fetch(conn.rpcEndpoint, { method: "POST", headers: { "content-type": "application/json" },
        signal: AbortSignal.timeout(5000),
        body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "getPriorityFeeEstimate",
          params: [{ accountKeys: accounts.map(a => a.toBase58()), options: { priorityLevel: "Medium" } }] }) });
      est = (await r.json())?.result?.priorityFeeEstimate ?? null;
    }
    if (est === null) {
      const fees = (await conn.getRecentPrioritizationFees({ lockedWritableAccounts: accounts })).map(f => f.prioritizationFee).sort((x, y) => x - y);
      est = fees.length ? fees[Math.floor(fees.length / 2)] : null;
    }
  } catch { est = null; }
  return Math.min(CU_CAP, Math.max(CU_FLOOR, Math.round(est ?? CU_FLOOR)));
}

/** Simulate for an exact CU limit (+10%), price at the medium estimate, send, rebroadcast; if not landed after
 * ~5s and the tx is idempotent (same request PDA, so a duplicate cannot also execute), re-sign at a higher price. */
export async function sendBuilt(conn: Connection, kp: Keypair, b: Built & { lookupTables?: AddressLookupTableAccount[] }, idempotent = true):
    Promise<{ sig: string; ok: boolean; err: unknown; feeLamports?: number | null; cuPrice?: number; cuLimit?: number }> {
  const sim = await conn.simulateTransaction(new VersionedTransaction(new TransactionMessage({
    payerKey: kp.publicKey, recentBlockhash: PublicKey.default.toBase58(),
    instructions: [ComputeBudgetProgram.setComputeUnitLimit({ units: 1_400_000 }), ...b.instructions] }).compileToV0Message(b.lookupTables)),
    { replaceRecentBlockhash: true, sigVerify: false });
  if (sim.value.err) throw new Error(`simulation failed ${JSON.stringify(sim.value.err)} ${(sim.value.logs ?? []).slice(-3).join(" | ")}`);
  const units = Math.ceil((sim.value.unitsConsumed ?? 200_000) * CU_MARGIN) + 300;  // +300 for the 2 budget ixs
  const writable = [...new Set(b.instructions.flatMap(ix => ix.keys.filter(k => k.isWritable).map(k => k.pubkey.toBase58())))]
    .slice(0, 20).map(k => new PublicKey(k));
  let price = await priorityFee(conn, writable);
  const { blockhash, lastValidBlockHeight } = await conn.getLatestBlockhash("confirmed");
  const build = (p: number) => {
    const tx = new VersionedTransaction(new TransactionMessage({ payerKey: kp.publicKey, recentBlockhash: blockhash,
      instructions: [ComputeBudgetProgram.setComputeUnitLimit({ units }), ComputeBudgetProgram.setComputeUnitPrice({ microLamports: p }),
                     ...b.instructions] }).compileToV0Message(b.lookupTables));
    tx.sign([kp]);
    return tx;
  };
  let tx = build(price);
  const sigs: string[] = [await conn.sendRawTransaction(tx.serialize(), { skipPreflight: false, maxRetries: 0 })];
  const t0 = Date.now();
  let escalated = false;
  for (;;) {
    await sleep(1500);
    let sts = (await conn.getSignatureStatuses(sigs)).value;
    if (sts.some(isLanded)) {
      // Several re-signed copies can land (same slot even): the first one to land does the work, the later ones fail
      // (e.g. Custom 3012 on an already-closed request). Report the copy that succeeded; failure only if none did.
      // 2026-10-08 05:12 MT BTC halt: the failed original was reported while the escalated copy had succeeded.
      for (let k = 0; k < 4 && sigs.length > 1 && !sts.some(st => isLanded(st) && st!.err === null); k++) {
        await sleep(1500);   // a sibling copy may still be landing: short grace before reporting a failure
        sts = (await conn.getSignatureStatuses(sigs, { searchTransactionHistory: true })).value;
      }
      let i = sts.findIndex(st => isLanded(st) && st!.err === null);
      if (i < 0) i = sts.findIndex(isLanded);
      const sig = sigs[i];
      let feeLamports: number | null = null;
      for (let k = 0; k < 5 && feeLamports === null; k++) {
        const t = await conn.getTransaction(sig, { maxSupportedTransactionVersion: 0, commitment: "confirmed" }).catch(() => null);
        feeLamports = t?.meta?.fee ?? null;
        if (feeLamports === null) await sleep(1000);
      }
      return { sig, ok: sts[i]!.err === null, err: sts[i]!.err, feeLamports, cuPrice: price, cuLimit: units };
    }
    if ((await conn.getBlockHeight("confirmed")) > lastValidBlockHeight) {
      const fin = (await conn.getSignatureStatuses(sigs, { searchTransactionHistory: true })).value;
      let j = fin.findIndex(st => st && st.err === null);   // prefer a copy that succeeded
      if (j < 0) j = fin.findIndex(Boolean);
      return j >= 0 ? { sig: sigs[j], ok: fin[j]!.err === null, err: fin[j]!.err, cuPrice: price, cuLimit: units }
                    : { sig: sigs[0], ok: false, err: "expired, not landed", cuPrice: price, cuLimit: units };
    }
    if (idempotent && !escalated && Date.now() - t0 > ESCALATE_AFTER_MS && price < CU_CAP) {
      price = Math.min(CU_CAP, price * ESCALATE_FACTOR);
      tx = build(price);
      escalated = true;
      sigs.push(await conn.sendRawTransaction(tx.serialize(), { skipPreflight: true, maxRetries: 0 }).catch(() => sigs[0]));
    }
    await conn.sendRawTransaction(tx.serialize(), { skipPreflight: true, maxRetries: 0 }).catch(() => {});
  }
}

/** Wait until a market request account is gone (keeper executed or rejected it). */
export async function awaitRequestGone(conn: Connection, req: PublicKey, timeoutMs = 90_000): Promise<number | null> {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    await sleep(2000);
    if (!(await conn.getAccountInfo(req))) return (Date.now() - t0) / 1000;
  }
  return null;
}
