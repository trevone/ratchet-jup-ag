import fs from "fs";
import { Keypair, TransactionMessage, VersionedTransaction, ComputeBudgetProgram } from "@solana/web3.js";
import { NATIVE_MINT, createCloseAccountInstruction, getAssociatedTokenAddressSync } from "@solana/spl-token";
import { connect } from "./jup";
(async () => {
  const kp = Keypair.fromSecretKey(Uint8Array.from(JSON.parse(fs.readFileSync("/home/box/.secrets/jup-trader.json", "utf8"))));
  const conn = await connect();
  const ata = getAssociatedTokenAddressSync(NATIVE_MINT, kp.publicKey);
  const bal = await conn.getTokenAccountBalance(ata).catch(() => null);
  console.log("wSOL ATA balance", bal?.value.uiAmountString, "lamports total", (await conn.getAccountInfo(ata))?.lamports);
  const { blockhash } = await conn.getLatestBlockhash("confirmed");
  const tx = new VersionedTransaction(new TransactionMessage({ payerKey: kp.publicKey, recentBlockhash: blockhash,
    instructions: [ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 1_000_000 }), createCloseAccountInstruction(ata, kp.publicKey, kp.publicKey)] }).compileToV0Message());
  tx.sign([kp]);
  const sim = await conn.simulateTransaction(tx);
  if (sim.value.err) { console.log("sim err", JSON.stringify(sim.value.err), sim.value.logs?.slice(-3)); return; }
  const sig = await conn.sendRawTransaction(tx.serialize());
  for (let i = 0; i < 20; i++) { await new Promise(r => setTimeout(r, 1500)); const s = (await conn.getSignatureStatuses([sig])).value[0];
    if (s?.confirmationStatus) { console.log("unwrap", s.err ? "FAILED" : "ok", `https://solscan.io/tx/${sig}`); break; }
    await conn.sendRawTransaction(tx.serialize(), { skipPreflight: true }).catch(() => {}); }
  console.log("SOL", (await conn.getBalance(kp.publicKey)) / 1e9, "ata exists", !!(await conn.getAccountInfo(ata)));
})();
