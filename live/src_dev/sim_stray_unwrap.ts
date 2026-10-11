// SIMULATION ONLY (never sends): increase shape with the stray-wSOL unwrap tail (close + re-create in the same tx).
import { PublicKey } from "@solana/web3.js";
import { BN } from "@coral-xyz/anchor";
import { NATIVE_MINT, createAssociatedTokenAccountIdempotentInstruction, createCloseAccountInstruction, getAssociatedTokenAddressSync } from "@solana/spl-token";
import { buildIncrease, connect, simulate } from "../src_next/jup";
(async () => {
  const conn = await connect();
  const owner = new PublicKey("Csa1b6D2h7i63ZgrBULLLL76B9uSBmYdKyP47UFCgwx");
  const b = await buildIncrease(conn, owner, "short", 90000000n, 73.5, 2550, NATIVE_MINT, new BN(10300000), "ETH");
  const ata = getAssociatedTokenAddressSync(NATIVE_MINT, owner);
  b.instructions.push(createCloseAccountInstruction(ata, owner, owner), createAssociatedTokenAccountIdempotentInstruction(owner, ata, owner, NATIVE_MINT));
  const r = await simulate(conn, owner, b);
  console.log(JSON.stringify({ ok: r.ok, err: r.err, units: r.unitsConsumed }));
})();
