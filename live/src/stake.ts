// SOL <-> JupSOL stake bucket for the bot wallet (Jupiter Swap API v1) + JupSOL transfer to the locked withdraw address.
// Destinations are hard-locked: swaps only to the owner's own token accounts; transfers only to WITHDRAW_ADDRESS.
// The key is never touched here (signing happens in send.ts).
import {
  AddressLookupTableAccount, ComputeBudgetProgram, Connection, PublicKey, SystemProgram, TransactionInstruction,
  TransactionMessage, VersionedTransaction,
} from "@solana/web3.js";
import {
  ASSOCIATED_TOKEN_PROGRAM_ID, NATIVE_MINT, TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID,
  createAssociatedTokenAccountIdempotentInstruction, createTransferCheckedInstruction, getAssociatedTokenAddressSync,
} from "@solana/spl-token";
import { ownerTokenAccounts, ownerTxDeltas } from "./jup";

export const JUPSOL_MINT = new PublicKey("jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v");
export const JUPSOL_POOL = new PublicKey("8VpRhuxa7sUUepdY3kQiTmX9rS5vx4WgaXiAnXq4KCtr");   // SPL stake pool; mint verified on read
export const JUP_V6 = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4";
export const WITHDRAW_ADDRESS = "59su4N4uaKcN8YZtYYgBj9kdUGytJgAF6HBTiyxzjGzp";   // only allowed external destination
// SOL in / SOL out (multi-asset, 2026-10-07): closes request SOL (desiredMint), but any USDC / ETH / WBTC that still lands
// in the wallet is swapped back to SOL ("sweep" direction). Only these input mints; output is always native SOL.
export const SWEEP_MINTS: Record<string, string> = {
  USDC: "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
  ETH: "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
  WBTC: "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh",
};

/** Owner's USDC / ETH / WBTC balances (classic token program ATAs), atoms as strings, plus the persistent wSOL ATA
 * ("WSOL": wrapped atoms; never swept, the sweep code only asks for USDC / ETH / WBTC). ``lamports`` / ``exists`` =
 * the account's rent deposit (owner capital, rent fix 2026-10-07). */
export async function readSweepTokens(conn: Connection, owner: PublicKey) {
  const mints: Record<string, string> = { ...SWEEP_MINTS, WSOL: NATIVE_MINT.toBase58() };
  const names = Object.keys(mints);
  const atas = names.map(n => getAssociatedTokenAddressSync(new PublicKey(mints[n]), owner));
  const accs = await conn.getMultipleAccountsInfo(atas);
  return Object.fromEntries(names.map((n, i) => [n, { mint: mints[n], ata: atas[i].toBase58(),
    atoms: accs[i] ? accs[i]!.data.readBigUInt64LE(64).toString() : "0", exists: !!accs[i], lamports: accs[i]?.lamports ?? 0 }]));
}
const DEFAULT_BASES = ["https://api.jup.ag/swap/v1", "https://lite-api.jup.ag/swap/v1"];
const HARD_MAX_SLIPPAGE_BPS = 300;
const ALLOWED_PROGRAMS = new Set([SystemProgram.programId.toBase58(), TOKEN_PROGRAM_ID.toBase58(),
  TOKEN_2022_PROGRAM_ID.toBase58(), ASSOCIATED_TOKEN_PROGRAM_ID.toBase58(), JUP_V6]);
const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));

export interface SwapBuilt { label: string; instructions: TransactionInstruction[]; positionRequest: PublicKey;
  lookupTables: AddressLookupTableAccount[]; info: Record<string, unknown> }

/** JupSOL balance of the owner's ATA + SOL/JupSOL rate from the stake pool account (total_lamports / pool_token_supply). */
export async function readStake(conn: Connection, owner: PublicKey) {
  const ata = getAssociatedTokenAddressSync(JUPSOL_MINT, owner);
  const [acc, pool] = await conn.getMultipleAccountsInfo([ata, JUPSOL_POOL]);
  const atoms = acc ? acc.data.readBigUInt64LE(64) : 0n;
  let rate: number | null = null;
  if (pool && new PublicKey(pool.data.subarray(162, 194)).equals(JUPSOL_MINT)) {
    const total = pool.data.readBigUInt64LE(258), supply = pool.data.readBigUInt64LE(266);
    if (supply > 0n) rate = Number(total) / Number(supply);
  }
  const jupsol = Number(atoms) / 1e9;
  return { mint: JUPSOL_MINT.toBase58(), ata: ata.toBase58(), ataExists: !!acc, ataLamports: acc?.lamports ?? 0, jupsolAtoms: atoms.toString(), jupsol,
           solPerJupsol: rate, solValue: rate === null ? null : Math.round(jupsol * rate * 1e9) / 1e9 };
}

async function jfetch(bases: string[], path: string, init?: RequestInit): Promise<any> {
  const errs: string[] = [];
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (process.env.JUP_API_KEY) headers["x-api-key"] = process.env.JUP_API_KEY;
  for (const base of bases) {
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const r = await fetch(base + path, { ...init, headers, signal: AbortSignal.timeout(15_000) });
        if (r.ok) return { base, body: await r.json() };
        errs.push(`${base} ${r.status}`);
        if (r.status !== 429) break;
        await sleep(1500);
      } catch (e) { errs.push(`${base} ${String(e).slice(0, 80)}`); break; }
    }
  }
  throw new Error(`jupiter api failed: ${errs.join("; ")}`);
}

export async function quote(a: { direction: "stake" | "unstake" | "sweep" | "swap"; amountAtoms: string; slippageBps: number;
                                 apiBases?: string[]; onlyDirectRoutes?: boolean; inputMint?: string; outputMint?: string;
                                 swapMode?: "ExactIn" | "ExactOut" }) {
  // ExactOut (2026-10-13, inter-bot loan repayment only): amountAtoms = exact OUTPUT atoms; slippage caps the input.
  const mode = a.swapMode ?? "ExactIn";
  if (mode !== "ExactIn" && !(mode === "ExactOut" && a.direction === "swap")) throw new Error(`swapMode ${mode} not allowed for ${a.direction}`);
  if (!(a.slippageBps >= 1 && a.slippageBps <= HARD_MAX_SLIPPAGE_BPS)) throw new Error(`slippageBps ${a.slippageBps} outside 1..${HARD_MAX_SLIPPAGE_BPS}`);
  if (!(BigInt(a.amountAtoms) > 0n)) throw new Error("amount must be > 0");
  if (a.direction === "sweep" && !Object.values(SWEEP_MINTS).includes(a.inputMint ?? "")) throw new Error(`sweep input mint ${a.inputMint} not allowed`);
  // "swap" (per-bot own-coin ledger, 2026-10-08 21:37 MT): any pair of the bot coins SOL / USDC / ETH / WBTC, plus
  // SOL / ETH / WBTC -> JupSOL (daily withdrawal). Output always lands in the owner's own account (checked in buildSwap).
  if (a.direction === "swap") {
    const ins = [NATIVE_MINT.toBase58(), ...Object.values(SWEEP_MINTS)], outs = [...ins, JUPSOL_MINT.toBase58()];
    if (!ins.includes(a.inputMint ?? "") || !outs.includes(a.outputMint ?? "") || a.inputMint === a.outputMint)
      throw new Error(`swap pair ${a.inputMint} -> ${a.outputMint} not allowed`);
  }
  const [inMint, outMint] = a.direction === "stake" ? [NATIVE_MINT, JUPSOL_MINT] : a.direction === "unstake" ? [JUPSOL_MINT, NATIVE_MINT]
    : a.direction === "sweep" ? [new PublicKey(a.inputMint!), NATIVE_MINT]
    : a.direction === "swap" ? [new PublicKey(a.inputMint!), new PublicKey(a.outputMint!)] : [null, null];
  if (!inMint || !outMint) throw new Error("direction must be stake|unstake|sweep|swap");
  const q = new URLSearchParams({ inputMint: inMint.toBase58(), outputMint: outMint.toBase58(), amount: a.amountAtoms,
    slippageBps: String(a.slippageBps), swapMode: mode, restrictIntermediateTokens: "true",
    onlyDirectRoutes: String(!!a.onlyDirectRoutes) });
  const { base, body } = await jfetch(a.apiBases?.length ? a.apiBases : DEFAULT_BASES, `/quote?${q}`);
  if (body.inputMint !== inMint.toBase58() || body.outputMint !== outMint.toBase58() || (mode === "ExactIn" ? body.inAmount : body.outAmount) !== a.amountAtoms
      || body.swapMode !== mode || Number(body.slippageBps) !== a.slippageBps) throw new Error(`quote mismatch ${JSON.stringify(body).slice(0, 200)}`);
  return { base, quote: body, inMint, outMint };
}

const toIx = (j: any) => new TransactionInstruction({ programId: new PublicKey(j.programId), data: Buffer.from(j.data, "base64"),
  keys: j.accounts.map((k: any) => ({ pubkey: new PublicKey(k.pubkey), isSigner: k.isSigner, isWritable: k.isWritable })) });

/** Quote + /swap-instructions, safety-checked: Jupiter v6 swap into the owner's own ATA, SOL transfers only into the
 * owner's wSOL ATA, no foreign programs. Jupiter's compute-budget ixs are dropped (send.ts prices the tx dynamically).
 * The owner's wSOL ATA (receiving account of the on-chain TP requests) is re-created at the end if the cleanup closed it.
 * Rent (2026-10-07): wSOL can only be unwrapped by closing its account (SPL Token has no partial unwrap), so with
 * wrapAndUnwrapSol Jupiter's cleanup closes the wSOL ATA and the appended idempotent create re-opens it IN THE SAME TX:
 * with the ATA already open (it is persistent now: buildIncrease no longer closes it) the rent leaves and returns within
 * the tx (net 0). The input USDC / ETH / WBTC ATA is never closed: it stays open at 0 for the next TP payout. */
export async function buildSwap(conn: Connection, owner: PublicKey, a: { direction: "stake" | "unstake" | "sweep" | "swap"; amountAtoms: string;
    slippageBps: number; maxPriceImpactPct?: number; apiBases?: string[]; onlyDirectRoutes?: boolean; inputMint?: string; outputMint?: string;
    swapMode?: "ExactIn" | "ExactOut" }): Promise<SwapBuilt> {
  const { base, quote: qr, inMint, outMint } = await quote(a);
  const impact = Number(qr.priceImpactPct ?? 0) * 100;   // API returns a fraction
  if (a.maxPriceImpactPct !== undefined && impact > a.maxPriceImpactPct) throw new Error(`price impact ${impact}% > ${a.maxPriceImpactPct}%`);
  const { body: si } = await jfetch([base], "/swap-instructions", { method: "POST", body: JSON.stringify({
    quoteResponse: qr, userPublicKey: owner.toBase58(), wrapAndUnwrapSol: true, dynamicComputeUnitLimit: false }) });
  if (si.error) throw new Error(`swap-instructions: ${si.error}`);
  if (si.tokenLedgerInstruction) throw new Error("unexpected token ledger instruction");
  const wsolAta = getAssociatedTokenAddressSync(NATIVE_MINT, owner);
  const outAta = getAssociatedTokenAddressSync(outMint, owner);
  const raw = [...(si.setupInstructions ?? []), si.swapInstruction, ...(si.cleanupInstruction ? [si.cleanupInstruction] : []),
               ...(si.otherInstructions ?? [])];
  const ixs = raw.map(toIx);
  if (si.swapInstruction.programId !== JUP_V6) throw new Error(`swap program ${si.swapInstruction.programId} not Jupiter v6`);
  for (const ix of ixs) {
    if (!ALLOWED_PROGRAMS.has(ix.programId.toBase58())) throw new Error(`foreign program ${ix.programId.toBase58()}`);
    if (ix.programId.equals(SystemProgram.programId)) {
      const isTransfer = ix.data.length >= 4 && ix.data.readUInt32LE(0) === 2;
      if (!isTransfer || !ix.keys[0].pubkey.equals(owner) || !ix.keys[1].pubkey.equals(wsolAta))
        throw new Error("system instruction other than SOL -> own wSOL ATA");
    }
  }
  const swapKeys = toIx(si.swapInstruction).keys.map(k => k.pubkey.toBase58());
  if (!swapKeys.includes(outAta.toBase58()) && !(outMint.equals(NATIVE_MINT) && swapKeys.includes(owner.toBase58())))
    throw new Error("swap output account is not the owner's own token account");
  ixs.push(createAssociatedTokenAccountIdempotentInstruction(owner, wsolAta, owner, NATIVE_MINT));
  const lookupTables = (await Promise.all((si.addressLookupTableAddresses ?? []).map((k: string) =>
    conn.getAddressLookupTable(new PublicKey(k)).then(r => r.value)))).filter(Boolean) as AddressLookupTableAccount[];
  const info = { direction: a.direction, api: base, inputMint: inMint.toBase58(), outputMint: outMint.toBase58(),
    inAmount: qr.inAmount, quotedOut: qr.outAmount, swapMode: qr.swapMode, minOut: qr.otherAmountThreshold, slippageBps: a.slippageBps,
    priceImpactPct: impact, route: (qr.routePlan ?? []).map((r: any) => r.swapInfo?.label).join(" > ") };
  return { label: `swap ${a.direction} ${qr.inAmount}`, instructions: ixs, positionRequest: owner, lookupTables, info };
}

/** JupSOL transfer to the locked withdraw address only (its ATA is created idempotently, rent paid by the bot). */
export async function buildSendJupsol(conn: Connection, owner: PublicKey, to: string, amountAtoms: string): Promise<SwapBuilt> {
  if (to !== WITHDRAW_ADDRESS) throw new Error("destination not allowed");
  if (!(BigInt(amountAtoms) > 0n)) throw new Error("amount must be > 0");
  const mintInfo = await conn.getAccountInfo(JUPSOL_MINT);
  if (!mintInfo) throw new Error("mint not found");
  const program = mintInfo.owner;
  if (!program.equals(TOKEN_PROGRAM_ID) && !program.equals(TOKEN_2022_PROGRAM_ID)) throw new Error("unexpected mint owner");
  const decimals = mintInfo.data[44];
  const toPk = new PublicKey(WITHDRAW_ADDRESS);
  const src = getAssociatedTokenAddressSync(JUPSOL_MINT, owner, false, program);
  const dst = getAssociatedTokenAddressSync(JUPSOL_MINT, toPk, false, program);
  const ixs = [createAssociatedTokenAccountIdempotentInstruction(owner, dst, toPk, JUPSOL_MINT, program),
               createTransferCheckedInstruction(src, JUPSOL_MINT, dst, owner, BigInt(amountAtoms), decimals, [], program)];
  return { label: `send ${amountAtoms} JupSOL atoms -> ${WITHDRAW_ADDRESS}`, instructions: ixs, positionRequest: toPk, lookupTables: [],
           info: { to: WITHDRAW_ADDRESS, toAta: dst.toBase58(), amountAtoms, decimals } };
}

export async function simulateBuilt(conn: Connection, payer: PublicKey, b: SwapBuilt) {
  const msg = new TransactionMessage({ payerKey: payer, recentBlockhash: PublicKey.default.toBase58(),
    instructions: [ComputeBudgetProgram.setComputeUnitLimit({ units: 1_400_000 }), ...b.instructions] }).compileToV0Message(b.lookupTables);
  const tx = new VersionedTransaction(msg);
  const sim = await conn.simulateTransaction(tx, { replaceRecentBlockhash: true, sigVerify: false });
  return { label: b.label, simulated: true, ok: sim.value.err === null, err: sim.value.err, unitsConsumed: sim.value.unitsConsumed,
           txBytes: tx.serialize().length, logs: (sim.value.logs ?? []).filter(l => /Error|error|failed/.test(l)).slice(0, 6) };
}

/** Exact balance changes of a confirmed tx for the owner: native lamports (fee payer), wSOL and JupSOL token atoms.
 * Rent fix 2026-10-07: ``rentDeltaLamports`` = rent moved into (+) / out of (-) the owner's token accounts (wSOL, USDC,
 * ETH, WBTC, JupSOL ATAs) by this tx; owner capital, so the sweep books native + wSOL atoms + rent as SOL, i.e. a
 * re-created token account is not a swap cost. ``tokenAccounts`` = per-account lamports / atoms changes. */
export async function txDeltas(conn: Connection, sig: string, owner: PublicKey) {
  let t: any = null;
  for (let k = 0; k < 8 && !t; k++) {
    t = await conn.getTransaction(sig, { maxSupportedTransactionVersion: 0, commitment: "confirmed" }).catch(() => null);
    if (!t) await sleep(1500);
  }
  if (!t?.meta) return null;
  const tok = (list: any[], mint: PublicKey) => list.filter(b => b.owner === owner.toBase58() && b.mint === mint.toBase58())
    .reduce((s, b) => s + BigInt(b.uiTokenAmount.amount), 0n);
  const m = t.meta;
  const own = ownerTxDeltas(t, owner.toBase58(), ownerTokenAccounts(owner, { JUPSOL: JUPSOL_MINT }));
  return { feeLamports: m.fee, nativeDeltaLamports: m.postBalances[0] - m.preBalances[0],
    rentDeltaLamports: own.rentLamports, tokenAccounts: own.accounts,
    wsolDeltaAtoms: (tok(m.postTokenBalances ?? [], NATIVE_MINT) - tok(m.preTokenBalances ?? [], NATIVE_MINT)).toString(),
    jupsolDeltaAtoms: (tok(m.postTokenBalances ?? [], JUPSOL_MINT) - tok(m.preTokenBalances ?? [], JUPSOL_MINT)).toString(),
    tokenDeltaAtoms: Object.fromEntries(Object.entries(SWEEP_MINTS).map(([n, mt]) => [n,
      (tok(m.postTokenBalances ?? [], new PublicKey(mt)) - tok(m.preTokenBalances ?? [], new PublicKey(mt))).toString()])),
    err: m.err };
}
