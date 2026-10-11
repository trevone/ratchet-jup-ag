// Read-only: dump ETH/BTC/SOL custody info (mint, decimals, oracles, leverage, fees) and oracle prices.
import { PublicKey } from "@solana/web3.js";
import { getAssociatedTokenAddressSync, NATIVE_MINT } from "@solana/spl-token";
import { connect, programs, POOL } from "./jup";
(async () => {
  const conn = await connect();
  const { perps, doves } = programs(conn);
  const pool: any = await perps.account.pool.fetch(POOL);
  const out: any = { poolCustodies: pool.custodies.map((c: PublicKey) => c.toBase58()) };
  for (const c of pool.custodies as PublicKey[]) {
    const cu: any = await perps.account.custody.fetch(c);
    let px: any = null;
    try { const f: any = await doves.account.agPriceFeed.fetch(cu.dovesAgOracle); px = { price: Number(f.price.toString()) * 10 ** f.expo, age: Math.round(Date.now()/1000 - Number(f.timestamp.toString())) }; } catch (e) { px = String(e).slice(0, 100); }
    out[c.toBase58()] = { mint: cu.mint.toBase58(), decimals: cu.decimals, isStable: cu.isStable, oracle: cu.oracle.oracleAccount.toBase58(),
      dovesOracle: cu.dovesOracle?.toBase58?.(), dovesAgOracle: cu.dovesAgOracle?.toBase58?.(), maxLeverage: cu.pricing.maxLeverage.toNumber() / 1e4,
      increaseBps: cu.increasePositionBps?.toString?.(), decreaseBps: cu.decreasePositionBps?.toString?.(), hourlyFundingDbps: cu.fundingRateState.hourlyFundingDbps.toString(),
      util: cu.assets.locked.toNumber() / cu.assets.owned.toNumber(), px,
      permissions: cu.permissions ? Object.fromEntries(Object.entries(cu.permissions).map(([k, v]: any) => [k, v])) : null };
  }
  const owner = new PublicKey("Csa1b6D2h7i63ZgrBULLLL76B9uSBmYdKyP47UFCgwx");
  const wsol = getAssociatedTokenAddressSync(NATIVE_MINT, owner);
  const ai = await conn.getAccountInfo(wsol);
  out.ownerWsolAta = { exists: !!ai, lamports: ai?.lamports, tokenAtoms: ai ? ai.data.readBigUInt64LE(64).toString() : null };
  console.log(JSON.stringify(out, null, 1));
})();
