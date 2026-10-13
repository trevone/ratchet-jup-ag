// Resting limit-open (increase) orders via Jupiter's hosted Perps API (perps-api.jup.ag), Trevor approved 2026-10-11 23:00 MT.
// The owner-only program instructions cannot open at a trigger price (createIncreasePositionMarketRequest is market-only);
// instantCreateLimitOrder needs Jupiter's keeper + apiKeeper co-signatures, so the API builds the tx, the owner signs it
// locally (key never leaves the droplet) and it is submitted through POST /transaction/execute (requireKeeperSignature).
// Allowed programs: Perps, compute budget, system, SPL token, ATA, and the oracle-price update Jupiter prepends (Ed25519
// signature verify + Doves price program; seen in the API's txs 2026-10-11).
// Every API tx is decoded and checked before signing: fee payer = owner, only expected programs, exactly one Perps
// instruction of the expected kind with the requested side / size / trigger / collateral, owner + position accounts match.
// Feature flags live in the Python config ([resting], default OFF); this file only adds CLI commands.
import { BorshInstructionCoder } from "@coral-xyz/anchor";
import { ComputeBudgetProgram, Connection, Ed25519Program, PublicKey, SystemProgram, VersionedTransaction } from "@solana/web3.js";
import { ASSOCIATED_TOKEN_PROGRAM_ID, TOKEN_PROGRAM_ID } from "@solana/spl-token";
import { IDL } from "./idl/jupiter-perpetuals-idl";
import { DOVES_PROGRAM_ID, MARKETS, Market, PERPS_PROGRAM_ID, USDC_MINT, positionPda } from "./jup";

export const PERPS_API = process.env.JUP_PERPS_API ?? "https://perps-api.jup.ag/v1";
const API_TIMEOUT_MS = 15_000;
const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));
const ALLOWED_PROGRAMS = new Set([PERPS_PROGRAM_ID, ComputeBudgetProgram.programId, SystemProgram.programId, TOKEN_PROGRAM_ID,
  ASSOCIATED_TOKEN_PROGRAM_ID, Ed25519Program.programId, DOVES_PROGRAM_ID].map(p => p.toBase58()));
const coder = new BorshInstructionCoder(IDL as any);

async function api(method: string, path: string, body?: unknown, query?: Record<string, string>): Promise<any> {
  const url = `${PERPS_API}${path}${query ? "?" + new URLSearchParams(query).toString() : ""}`;
  const r = await fetch(url, { method, headers: { "content-type": "application/json" }, signal: AbortSignal.timeout(API_TIMEOUT_MS),
    body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await r.text();
  let j: any;
  try { j = JSON.parse(text); } catch { j = { code: `http_${r.status}`, message: text.slice(0, 300) }; }
  if (!r.ok || (j && j.code && !j.serializedTxBase64 && !j.txid && !j.dataList)) return { apiError: true, status: r.status, ...j };
  return j;
}

export interface LimitReq { side: "long" | "short"; inputMint: string; inputAtoms: string; sizeUsd: number; triggerPriceUsd: number }

function u(x: any): string { return x?.toString?.() ?? String(x); }

/** Decode + check an API transaction before the owner signs it. kind: "create" | "cancel". */
export async function verifyTx(conn: Connection, owner: PublicKey, tx: VersionedTransaction, kind: "create" | "cancel",
                               mkt: Market, exp: { side?: string; sizeUsd?: number; triggerPriceUsd?: number; inputAtoms?: string;
                               request?: string }) {
  const msg = tx.message;
  const alts = [];
  for (const l of msg.addressTableLookups) {
    const a = (await conn.getAddressLookupTable(l.accountKey)).value;
    if (!a) throw new Error(`lookup table ${l.accountKey.toBase58()} not found`);
    alts.push(a);
  }
  const keys = msg.getAccountKeys({ addressLookupTableAccounts: alts });
  const problems: string[] = [];
  if (!keys.get(0)?.equals(owner)) problems.push(`fee payer ${keys.get(0)?.toBase58()} != owner`);
  const perps: { name: string; data: any; accounts: PublicKey[] }[] = [];
  for (const ix of msg.compiledInstructions) {
    const prog = keys.get(ix.programIdIndex)!;
    const accs = ix.accountKeyIndexes.map(i => keys.get(i)!);
    if (!ALLOWED_PROGRAMS.has(prog.toBase58())) { problems.push(`unexpected program ${prog.toBase58()}`); continue; }
    if (prog.equals(PERPS_PROGRAM_ID)) {
      const d = coder.decode(Buffer.from(ix.data));
      perps.push({ name: d?.name ?? "?", data: d?.data, accounts: accs });
    } else if (prog.equals(SystemProgram.programId)) {
      const t = Buffer.from(ix.data).readUInt32LE(0);
      if (t === 2 && !accs[0].equals(owner)) problems.push("system transfer not from owner");
    } else if (prog.equals(TOKEN_PROGRAM_ID)) {
      const t = ix.data[0];
      if (t === 9 && !accs[1].equals(owner)) problems.push("token close-account destination is not the owner");
      if ((t === 3 || t === 12 || t === 4 || t === 6 || t === 7) && kind === "cancel") problems.push(`token instruction ${t} in a cancel`);
    }
  }
  if (kind === "create" && perps[0] && Object.keys(perps[0].data?.params?.side ?? {})[0] && perps[0].data?.params?.triggerAboveThreshold !== undefined) {
    const above = !!perps[0].data.params.triggerAboveThreshold;   // long add fires at/below the trigger, short at/above
    if (above !== (exp.side === "short")) problems.push(`triggerAboveThreshold ${above} wrong for a ${exp.side} add`);
  }
  const want = kind === "create" ? ["instantCreateLimitOrder"] : ["closePositionRequest2", "closePositionRequest"];
  if (perps.length !== 1) problems.push(`expected exactly 1 Perps instruction, got ${perps.length}`);
  const p = perps[0];
  let positionRequest: string | null = null;
  if (p) {
    if (!want.includes(p.name)) problems.push(`Perps instruction ${p.name} not in ${want.join("/")}`);
    const names: string[] = ((IDL as any).instructions.find((i: any) => i.name === p.name)?.accounts ?? []).map((a: any) => a.name);
    const acc = (n: string) => { const i = names.indexOf(n); return i >= 0 ? p.accounts[i] : undefined; };
    if (!acc("owner")?.equals(owner)) problems.push("Perps owner account != owner");
    positionRequest = acc("positionRequest")?.toBase58() ?? null;
    if (kind === "create") {
      const prm = p.data?.params ?? {};
      const side = Object.keys(prm.side ?? {})[0];
      if (side !== exp.side) problems.push(`side ${side} != ${exp.side}`);
      if (u(prm.sizeUsdDelta) !== String(Math.round((exp.sizeUsd ?? 0) * 1e6))) problems.push(`sizeUsdDelta ${u(prm.sizeUsdDelta)} != ${exp.sizeUsd}`);
      if (u(prm.triggerPrice) !== String(Math.round((exp.triggerPriceUsd ?? 0) * 1e6))) problems.push(`triggerPrice ${u(prm.triggerPrice)} != ${exp.triggerPriceUsd}`);
      if (u(prm.collateralTokenDelta) !== String(exp.inputAtoms)) problems.push(`collateralTokenDelta ${u(prm.collateralTokenDelta)} != ${exp.inputAtoms}`);
      const pos = positionPda(owner, exp.side as any, mkt);
      if (!acc("position")?.equals(pos)) problems.push(`position ${acc("position")?.toBase58()} != ${pos.toBase58()}`);
      if (!acc("custody")?.equals(MARKETS[mkt].custody)) problems.push("custody is not this market");
    } else if (positionRequest !== exp.request) problems.push(`positionRequest ${positionRequest} != ${exp.request}`);
  }
  return { ok: problems.length === 0, problems, perpsInstruction: p?.name ?? null, positionRequest,
           params: p?.data?.params ? Object.fromEntries(Object.entries(p.data.params).map(([k, v]: any) =>
             [k, typeof v === "object" && v && !v.toString?.().match(/^\d+$/) ? Object.keys(v)[0] ?? u(v) : u(v)])) : null };
}

export async function listLimitOrders(owner: PublicKey) {
  const r = await api("GET", "/orders/limit", undefined, { walletAddress: owner.toBase58() });
  if (r.apiError) return { ok: false, error: r.message ?? r.code, code: r.code };
  return { ok: true, count: r.count, orders: r.dataList };
}

/** Build (API) + verify a create-limit-order tx. Never signs or sends. */
export async function buildLimit(conn: Connection, owner: PublicKey, mkt: Market, a: LimitReq) {
  const marketMint = MARKETS[mkt].mint.toBase58();
  const collateralMint = a.side === "long" ? marketMint : USDC_MINT.toBase58();
  const body = { collateralMint, collateralTokenDelta: String(a.inputAtoms), inputMint: a.inputMint, marketMint, side: a.side,
                 sizeUsdDelta: String(Math.round(a.sizeUsd * 1e6)), triggerPrice: String(Math.round(a.triggerPriceUsd * 1e6)),
                 walletAddress: owner.toBase58() };
  const r = await api("POST", "/orders/limit", body);
  if (r.apiError) return { ok: false, refused: true, code: r.code, error: r.message ?? r.code, request: body };
  const tx = VersionedTransaction.deserialize(Buffer.from(r.serializedTxBase64, "base64"));
  const v = await verifyTx(conn, owner, tx, "create", mkt, { side: a.side, sizeUsd: a.sizeUsd, triggerPriceUsd: a.triggerPriceUsd,
                                                             inputAtoms: String(a.inputAtoms) });
  return { ok: v.ok, tx, verify: v, quote: r.quote, txMetadata: r.txMetadata, requireKeeperSignature: r.requireKeeperSignature,
           positionRequest: r.positionRequestPubkey ?? v.positionRequest, apiPositionRequest: r.positionRequestPubkey ?? null,
           request: body, error: v.ok ? undefined : `tx check failed: ${v.problems.join("; ")}` };
}

export async function buildCancelLimit(conn: Connection, owner: PublicKey, mkt: Market, request: string) {
  const r = await api("DELETE", "/orders/limit", { positionRequestPubkey: request });
  if (r.apiError) return { ok: false, refused: true, code: r.code, error: r.message ?? r.code };
  const tx = VersionedTransaction.deserialize(Buffer.from(r.serializedTxBase64, "base64"));
  const v = await verifyTx(conn, owner, tx, "cancel", mkt, { request });
  return { ok: v.ok, tx, verify: v, requireKeeperSignature: r.requireKeeperSignature, txMetadata: r.txMetadata,
           error: v.ok ? undefined : `tx check failed: ${v.problems.join("; ")}` };
}

/** Simulate an API tx (signature checks off: the keeper signatures are added by Jupiter at execute). Never sends. */
export async function simulateApiTx(conn: Connection, tx: VersionedTransaction) {
  const sim = await conn.simulateTransaction(tx, { sigVerify: false, replaceRecentBlockhash: true });
  const logs = sim.value.logs ?? [];
  return { simOk: sim.value.err === null, simErr: sim.value.err, unitsConsumed: sim.value.unitsConsumed,
           logs: logs.filter(l => /Instruction:|Error|error|failed/.test(l)).slice(0, 10) };
}

/** LIVE: owner signs the verified API tx and submits it (keeper-signed txs through POST /transaction/execute, else raw). */
export async function signAndSubmit(conn: Connection, kp: any, tx: VersionedTransaction, action: string, keeper: boolean) {
  tx.sign([kp]);
  let sig: string;
  if (keeper) {
    const r = await api("POST", "/transaction/execute", { action, serializedTxBase64: Buffer.from(tx.serialize()).toString("base64") });
    if (r.apiError || !r.txid) return { ok: false, sent: true, error: r.message ?? r.code ?? "no txid", code: r.code };
    sig = r.txid;
  } else {
    sig = await conn.sendRawTransaction(tx.serialize(), { skipPreflight: false, maxRetries: 3 });
  }
  const t0 = Date.now();
  while (Date.now() - t0 < 60_000) {
    await sleep(1500);
    const st = (await conn.getSignatureStatuses([sig], { searchTransactionHistory: true })).value[0];
    if (st && (st.confirmationStatus === "confirmed" || st.confirmationStatus === "finalized")) {
      let feeLamports: number | null = null;
      for (let k = 0; k < 5 && feeLamports === null; k++) {
        const t = await conn.getTransaction(sig, { maxSupportedTransactionVersion: 0, commitment: "confirmed" }).catch(() => null);
        feeLamports = t?.meta?.fee ?? null;
        if (feeLamports === null) await sleep(1000);
      }
      return { ok: st.err === null, sent: true, sig, err: st.err, feeLamports };
    }
  }
  return { ok: false, sent: true, sig, err: "not confirmed within 60 s" };
}
