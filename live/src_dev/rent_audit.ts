// READ-ONLY audit: owner token accounts (lamports / amounts) + per-tx owner-account lamport deltas for given sigs.
import { PublicKey } from "@solana/web3.js";
import { TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID } from "@solana/spl-token";
import { connect } from "../src/jup";
const OWNER = new PublicKey(process.env.JUP_OWNER ?? "Csa1b6D2h7i63ZgrBULLLL76B9uSBmYdKyP47UFCgwx");
(async () => {
  const conn = await connect();
  const out: any = { owner: OWNER.toBase58(), nativeLamports: await conn.getBalance(OWNER), tokenAccounts: [] };
  const mine = new Set<string>();
  for (const pid of [TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID]) {
    const r = await conn.getParsedTokenAccountsByOwner(OWNER, { programId: pid });
    for (const a of r.value) {
      const info: any = (a.account.data as any).parsed.info;
      mine.add(a.pubkey.toBase58());
      out.tokenAccounts.push({ pubkey: a.pubkey.toBase58(), program: pid.toBase58().slice(0, 6), mint: info.mint,
        amount: info.tokenAmount.amount, lamports: a.account.lamports, closeAuthority: info.closeAuthority ?? null });
    }
  }
  out.rentExempt165 = await conn.getMinimumBalanceForRentExemption(165);
  const sigs = process.argv.slice(2);
  out.txs = [];
  for (const s of sigs) {
    const t: any = await conn.getTransaction(s, { maxSupportedTransactionVersion: 0, commitment: "confirmed" });
    if (!t?.meta) { out.txs.push({ sig: s, missing: true }); continue; }
    const keys = [...t.transaction.message.staticAccountKeys.map((k: PublicKey) => k.toBase58()),
      ...(t.meta.loadedAddresses?.writable ?? []).map(String), ...(t.meta.loadedAddresses?.readonly ?? []).map(String)];
    const deltas: any = {};
    keys.forEach((k: string, i: number) => {
      const d = t.meta.postBalances[i] - t.meta.preBalances[i];
      if (k === OWNER.toBase58() || mine.has(k) || (d !== 0 && Math.abs(d) < 3_000_000 && Math.abs(d) > 1_000_000))
        deltas[k] = { pre: t.meta.preBalances[i], post: t.meta.postBalances[i], d };
    });
    const tokOwner = (l: any[]) => (l ?? []).filter((b: any) => b.owner === OWNER.toBase58()).map((b: any) => ({ i: b.accountIndex, acct: keys[b.accountIndex], mint: b.mint, amt: b.uiTokenAmount.amount }));
    out.txs.push({ sig: s, slot: t.slot, blockTime: t.blockTime, fee: t.meta.fee, payer: keys[0], deltas,
      preTok: tokOwner(t.meta.preTokenBalances), postTok: tokOwner(t.meta.postTokenBalances),
      logs: (t.meta.logMessages ?? []).filter((l: string) => /Instruction: (Create|Close|InitializeAccount|SyncNative|Route|SharedAccountsRoute|ExactOutRoute)|CloseAccount|InitializeAccount3/.test(l)).slice(0, 20) });
  }
  console.log(JSON.stringify(out, null, 1));
})().catch(e => { console.log(JSON.stringify({ ok: false, error: String(e).slice(0, 300) })); process.exit(1); });
