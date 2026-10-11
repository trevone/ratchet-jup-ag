// READ-ONLY: txDeltas (staged code) for given sigs.
import { PublicKey } from "@solana/web3.js";
import { connect } from "../src_next/jup";
import { txDeltas } from "../src_next/stake";
(async () => {
  const conn = await connect();
  const owner = new PublicKey("Csa1b6D2h7i63ZgrBULLLL76B9uSBmYdKyP47UFCgwx");
  for (const s of process.argv.slice(2)) console.log(s.slice(0, 8), JSON.stringify(await txDeltas(conn, s, owner)));
})();
