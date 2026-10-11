// Read-only validation: simulate (never send, sigVerify off) a TP trigger decrease with desiredMint = SOL on an EXISTING
// third-party ETH/BTC long and short position, to prove the request shape our bot will use is accepted for swapped payouts.
import { PublicKey } from "@solana/web3.js";
import { buildDecrease, connect, programs, positionPda, MARKETS, simulate, Market, USDC_CUSTODY } from "./jup";
(async () => {
  const conn = await connect();
  const { perps } = programs(conn);
  for (const m of ["ETH", "BTC"] as Market[]) for (const side of ["long", "short"] as const) {
    const coll = side === "long" ? MARKETS[m].custody : USDC_CUSTODY;
    const accs = await conn.getProgramAccounts(perps.programId, { dataSlice: { offset: 8, length: 161 }, filters: [
      { memcmp: { offset: 72, bytes: MARKETS[m].custody.toBase58() } }, { memcmp: { offset: 104, bytes: coll.toBase58() } }] });
    let done = false;
    const open = accs.filter(a => a.account.data.readBigUInt64LE(153) > 0n);
    for (const a of open.slice(0, 10)) {
      const owner = new PublicKey(a.account.data.subarray(0, 32));
      if (!positionPda(owner, side, m).equals(a.pubkey)) continue;
      const p: any = await perps.account.position.fetch(a.pubkey);
      if (p.sizeUsd.isZero()) continue;
      const px = p.price.toNumber() / 1e6;
      const b = await buildDecrease(conn, owner, side, { triggerPriceUsd: Math.round(px * (side === "long" ? 1.5 : 0.5) * 100) / 100, triggerAbove: side === "long",
                                                         sizeUsd: Math.min(10, p.sizeUsd.toNumber() / 1e6 / 2) }, m);
      const r = await simulate(conn, owner, b);
      console.log(JSON.stringify({ m, side, n: accs.length, ok: r.ok, err: r.err, logs: r.logs.filter((l: string) => /Instruction:|Error/.test(l)) }));
      done = true; break;
    }
    if (!done) console.log(JSON.stringify({ m, side, n: accs.length, note: "no open position found in sample" }));
  }
})();
