import { PublicKey } from "@solana/web3.js";
import { TOKEN_PROGRAM_ID } from "@solana/spl-token";
import { connect } from "./jup";
(async () => {
  const conn = await connect();
  const owner = new PublicKey("Csa1b6D2h7i63ZgrBULLLL76B9uSBmYdKyP47UFCgwx");
  const r = await conn.getParsedTokenAccountsByOwner(owner, { programId: TOKEN_PROGRAM_ID });
  console.log(JSON.stringify(r.value.map(a => ({ ata: a.pubkey.toBase58(), mint: a.account.data.parsed.info.mint, amt: a.account.data.parsed.info.tokenAmount.uiAmountString, lamports: a.account.lamports }))));
})();
