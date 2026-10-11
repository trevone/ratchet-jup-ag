// Read-only: list txs since a unix time where the owner's native SOL rose and the owner did NOT sign (external deposits).
import { PublicKey } from "@solana/web3.js";
import { connect } from "./jup";
const OWNER = new PublicKey(process.env.JUP_OWNER ?? "Csa1b6D2h7i63ZgrBULLLL76B9uSBmYdKyP47UFCgwx");
(async () => {
  const since = Number(process.argv[2] ?? 0);
  const conn = await connect();
  const sigs = (await conn.getSignaturesForAddress(OWNER, { limit: 200 }, "confirmed")).filter(s => (s.blockTime ?? 0) >= since && !s.err);
  for (const s of sigs) {
    const t: any = await conn.getTransaction(s.signature, { maxSupportedTransactionVersion: 0, commitment: "confirmed" });
    if (!t?.meta) continue;
    const keys = t.transaction.message.staticAccountKeys.map((k: PublicKey) => k.toBase58());
    const i = keys.indexOf(OWNER.toBase58());
    const nSigners = t.transaction.message.header.numRequiredSignatures;
    const signed = i >= 0 && i < nSigners;
    const d = i >= 0 ? (t.meta.postBalances[i] - t.meta.preBalances[i]) / 1e9 : 0;
    if (!signed && d > 0.01) console.log(JSON.stringify({ sig: s.signature, blockTime: t.blockTime, from: keys[0], sol: d }));
  }
})();
