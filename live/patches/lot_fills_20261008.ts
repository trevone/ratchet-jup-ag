// READ-ONLY: list Increase/Decrease position events (fill price, size, collateral) of one position since its open tx.
import { BorshEventCoder } from "@coral-xyz/anchor";
import bs58 from "bs58";
import { PublicKey } from "@solana/web3.js";
import { connect } from "../src/jup";
import { IDL } from "../src/idl/jupiter-perpetuals-idl";
const PERPS = "PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu";
const TAG = Buffer.from([0xe4, 0x45, 0xa5, 0x2e, 0x51, 0xcb, 0x9a, 0x1d]);
async function main() {
  const [position, sinceSig] = process.argv.slice(2);
  const conn = await connect();
  const first = await conn.getTransaction(sinceSig, { maxSupportedTransactionVersion: 0, commitment: "confirmed" });
  const sigs = (await conn.getSignaturesForAddress(new PublicKey(position), { limit: 1000 }, "confirmed")).filter(s => s.slot >= first!.slot);
  const coder = new BorshEventCoder(IDL as any);
  const out: any[] = [];
  for (const { signature } of sigs) {
    const t: any = await conn.getTransaction(signature, { maxSupportedTransactionVersion: 0, commitment: "confirmed" });
    if (!t?.meta || t.meta.err) continue;
    const keys: string[] = [...t.transaction.message.staticAccountKeys.map((k: any) => k.toBase58()),
      ...(t.meta.loadedAddresses?.writable ?? []).map(String), ...(t.meta.loadedAddresses?.readonly ?? []).map(String)];
    for (const inner of t.meta.innerInstructions ?? []) for (const ix of inner.instructions) {
      if (keys[ix.programIdIndex] !== PERPS) continue;
      const data = Buffer.from(bs58.decode(ix.data));
      if (data.length < 16 || !data.subarray(0, 8).equals(TAG)) continue;
      const ev: any = coder.decode(data.subarray(8).toString("base64"));
      if (!ev || ev.data?.positionKey?.toBase58?.() !== position) continue;
      if (!/Increase|Decrease|Liquidate/.test(ev.name) || /PreSwap/.test(ev.name)) continue;
      const n = (x: any) => (x ? Number(x.toString()) / 1e6 : null);
      out.push({ ev: ev.name, time: new Date(t.blockTime * 1000).toISOString(), sig: signature.slice(0, 10), price: n(ev.data.price),
        sizeUsdDelta: n(ev.data.sizeUsdDelta), collateralUsdDelta: n(ev.data.collateralUsdDelta), positionSizeUsd: n(ev.data.positionSizeUsd),
        feeUsd: n(ev.data.feeUsd) });
    }
  }
  console.log(JSON.stringify(out.sort((a, b) => a.time.localeCompare(b.time))));
}
main().catch(e => { console.log(JSON.stringify({ ok: false, error: String(e).slice(0, 300) })); process.exit(1); });
