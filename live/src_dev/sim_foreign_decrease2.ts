// Read-only: simulate (never send) decrease variants on third-party open positions to learn which desiredMint is allowed.
import { PublicKey } from "@solana/web3.js";
import { NATIVE_MINT } from "@solana/spl-token";
import { buildDecrease, connect, programs, positionPda, MARKETS, simulate, Market, USDC_CUSTODY } from "./jup";
(async () => {
  const conn = await connect();
  const { perps } = programs(conn);
  const usdcMint: PublicKey = (await perps.account.custody.fetch(USDC_CUSTODY) as any).mint;
  for (const m of ["SOL", "ETH", "BTC"] as Market[]) for (const side of ["long", "short"] as const) {
    if (m === "SOL" && side === "long") continue;
    const coll = side === "long" ? MARKETS[m].custody : USDC_CUSTODY;
    const collMint: PublicKey = (await perps.account.custody.fetch(coll) as any).mint;
    const accs = await conn.getProgramAccounts(perps.programId, { dataSlice: { offset: 8, length: 161 }, filters: [
      { memcmp: { offset: 72, bytes: MARKETS[m].custody.toBase58() } }, { memcmp: { offset: 104, bytes: coll.toBase58() } }] });
    const open = accs.filter(a => a.account.data.readBigUInt64LE(153) > 0n);
    for (const a of open.slice(0, 10)) {
      const owner = new PublicKey(a.account.data.subarray(0, 32));
      if (!positionPda(owner, side, m).equals(a.pubkey)) continue;
      const p: any = await perps.account.position.fetch(a.pubkey);
      if (p.sizeUsd.isZero()) continue;
      const px = p.price.toNumber() / 1e6;
      const tp = Math.round(px * (side === "long" ? 1.5 : 0.5) * 100) / 100;
      const sz = Math.min(10, p.sizeUsd.toNumber() / 1e6 / 2);
      const variants: [string, any][] = [
        ["trigger desired=SOL", { triggerPriceUsd: tp, triggerAbove: side === "long", sizeUsd: sz, desiredMint: NATIVE_MINT }],
        ["trigger desired=collateral", { triggerPriceUsd: tp, triggerAbove: side === "long", sizeUsd: sz, desiredMint: collMint }],
        ["trigger entire desired=collateral", { triggerPriceUsd: tp, triggerAbove: side === "long", entire: true, desiredMint: collMint }],
        ["market desired=SOL", { priceSlippageUsd: side === "long" ? px * 0.5 : px * 1.5, sizeUsd: sz, desiredMint: NATIVE_MINT }],
      ];
      for (const [name, o] of variants) {
        const b = await buildDecrease(conn, owner, side, o, m);
        const r = await simulate(conn, owner, b);
        console.log(JSON.stringify({ m, side, name, ok: r.ok, err: r.err, logs: r.logs.filter((l: string) => /Error|failed/.test(l)).slice(0, 2) }));
      }
      break;
    }
  }
})();
