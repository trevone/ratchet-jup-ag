// One-off READ-ONLY check (2026-10-08 18:05 MT): can ETH/BTC decreases pay out SOL (desiredMint = wSOL)? Simulation only, never sends.
import { NATIVE_MINT } from "@solana/spl-token";
import { buildDecrease, connect, simulate } from "../src/jup";
import { PublicKey } from "@solana/web3.js";
const OWNER = new PublicKey(process.env.JUP_OWNER ?? "Csa1b6D2h7i63ZgrBULLLL76B9uSBmYdKyP47UFCgwx");
(async () => {
  const conn = await connect();
  for (const [mkt, side, px, above] of [["ETH", "short", 2400, false], ["BTC", "long", 85000, true]] as const) {
    for (const entire of [false, true]) {
      const b = await buildDecrease(conn, OWNER, side, { triggerPriceUsd: px, triggerAbove: above, entire, sizeUsd: entire ? undefined : 10, desiredMint: NATIVE_MINT }, mkt as any);
      const r: any = await simulate(conn, OWNER, b);
      console.log(mkt, side, entire ? "entire" : "$10", "desiredMint=SOL ->", JSON.stringify({ ok: r.ok, err: r.err ?? r.error, logs: (r.logs || []).filter((l: string) => /Error|error|6018|InvalidMint/.test(l)).slice(0, 3) }));
    }
  }
})().catch(e => { console.log("ERR", String(e).slice(0, 300)); process.exit(1); });
