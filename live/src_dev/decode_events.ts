// READ-ONLY: decode Jupiter Perps anchor CPI events (emit_cpi) of given txs.
import { BorshEventCoder } from "@coral-xyz/anchor";
import { PublicKey } from "@solana/web3.js";
import bs58 from "bs58";
import { connect, PERPS_PROGRAM_ID } from "../src/jup";
import { IDL } from "../src/idl/jupiter-perpetuals-idl";
const TAG = Buffer.from("e445a52e51cb9a1d", "hex");   // anchor EVENT_IX_TAG (le)
(async () => {
  const conn = await connect();
  const coder = new BorshEventCoder(IDL as any);
  for (const s of process.argv.slice(2)) {
    const t: any = await conn.getTransaction(s, { maxSupportedTransactionVersion: 0, commitment: "confirmed" });
    const keys = [...t.transaction.message.staticAccountKeys.map((k: PublicKey) => k.toBase58()),
      ...(t.meta.loadedAddresses?.writable ?? []).map(String), ...(t.meta.loadedAddresses?.readonly ?? []).map(String)];
    for (const inner of t.meta.innerInstructions ?? []) for (const ix of inner.instructions) {
      if (keys[ix.programIdIndex] !== PERPS_PROGRAM_ID.toBase58()) continue;
      const data = Buffer.from(bs58.decode(ix.data));
      if (!data.subarray(0, 8).equals(TAG)) continue;
      const ev = coder.decode(data.subarray(8).toString("base64"));
      if (!ev) continue;
      const d: any = {};
      for (const [k, v] of Object.entries(ev.data as any)) d[k] = (v as any)?.toString?.() ?? v;
      console.log(s.slice(0, 8), ev.name, JSON.stringify(d));
    }
  }
})();
