// Read-only: print network fee (lamports) for each signature given on argv.
import { connect } from "./jup";
(async () => {
  const conn = await connect();
  const out: Record<string, number | null> = {};
  for (const s of process.argv.slice(2)) {
    const t = await conn.getTransaction(s, { maxSupportedTransactionVersion: 0, commitment: "confirmed" }).catch(() => null);
    out[s] = t?.meta?.fee ?? null;
  }
  console.log(JSON.stringify(out));
})();
