import { makeWASocket, DisconnectReason, initAuthCreds, proto, BufferJSON } from '@whiskeysockets/baileys';
import express from 'express';
import cors from 'cors';
import readline from 'readline';
import fs from 'fs';
import { createClient } from '@supabase/supabase-js';

// --- YOUR SUPABASE CREDENTIALS ---
// Replace these with your actual Supabase URL and SERVICE ROLE KEY
const SUPABASE_URL = "https://atkcgxthfgpadgxgqeaj.supabase.co";
const SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImF0a2NneHRoZmdwYWRneGdxZWFqIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MjIwMjM2MiwiZXhwIjoyMDk3Nzc4MzYyfQ.t6oHYWOGiOagTkokdSgz5_Jn8R6P44Z5Tsp7IHvuHJ0";
const supabase = createClient(SUPABASE_URL, SUPABASE_KEY);

// --- FORCE CLEAN STATE ---
// This automatically deletes the corrupted session folder so you don't have to do it manually.
if (fs.existsSync('./auth_info_baileys')) {
    fs.rmSync('./auth_info_baileys', { recursive: true, force: true });
    console.log("🗑️ Cleared corrupted local session data.");
}

const app = express();
app.use(express.json());
app.use(cors());

// Keep-Alive Endpoint for UptimeRobot
app.get('/', (req, res) => {
    res.status(200).send('WhatsApp Engine is Awake!');
});

let sock = null;

const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
const question = (text) => new Promise((resolve) => rl.question(text, resolve));

// Anti-Ban delay function
const delay = ms => new Promise(res => setTimeout(res, ms));

// ==========================================
// THE SUPABASE CUSTOM AUTH ADAPTER
// ==========================================
async function useSupabaseAuthState() {
    const writeData = async (data, id) => {
        try {
            // Safely stringify the Buffers, then parse back to JSON for Supabase JSONB
            const serialized = JSON.parse(JSON.stringify(data, BufferJSON.replacer));
            await supabase.from('whatsapp_session').upsert({ id: id, data: serialized });
        } catch (error) {
            console.error("Error writing to Supabase:", error);
        }
    };

    const readData = async (id) => {
        try {
            const { data, error } = await supabase.from('whatsapp_session').select('data').eq('id', id).single();
            if (error || !data) return null;
            
            // Reconstruct the raw Buffers from the JSON database entry
            return JSON.parse(JSON.stringify(data.data), BufferJSON.reviver);
        } catch (error) {
            return null;
        }
    };

    const removeData = async (id) => {
        try {
            await supabase.from('whatsapp_session').delete().eq('id', id);
        } catch (error) {
            console.error("Error removing from Supabase:", error);
        }
    };

    const creds = (await readData('creds')) || initAuthCreds();

    return {
        state: {
            creds,
            keys: {
                get: async (type, ids) => {
                    const data = {};
                    await Promise.all(
                        ids.map(async (id) => {
                            let value = await readData(`${type}-${id}`);
                            if (type === 'app-state-sync-key' && value) {
                                value = proto.Message.AppStateSyncKeyData.fromObject(value);
                            }
                            data[id] = value;
                        })
                    );
                    return data;
                },
                set: async (data) => {
                    const tasks = [];
                    for (const category in data) {
                        for (const id in data[category]) {
                            const value = data[category][id];
                            const key = `${category}-${id}`;
                            tasks.push(value ? writeData(value, key) : removeData(key));
                        }
                    }
                    await Promise.all(tasks);
                }
            }
        },
        saveCreds: () => writeData(creds, 'creds')
    };
}

async function connectToWhatsApp() {
    console.log("Starting WhatsApp Engine via Supabase...");
    
    const { state, saveCreds } = await useSupabaseAuthState();
    
    // The ABSOLUTE BARE MINIMUM config for v7 Pairing Codes.
    // No browser spoofing, no version spoofing.
    sock = makeWASocket({
        auth: state
    });

    sock.ev.on('creds.update', saveCreds);

    sock.ev.on('connection.update', async (update) => {
        const { connection, lastDisconnect, qr } = update;
        
        // When WhatsApp sends the QR event, we intercept it and ask for the pairing code instead.
        if (qr && !sock.authState.creds.registered) {
            console.log("\n=================================================");
            const number = await question('Enter your College WhatsApp Phone Number (e.g. 254712345678): ');
            console.log("=================================================");
            
            try {
                let code = await sock.requestPairingCode(number.trim());
                code = code?.match(/.{1,4}/g)?.join('-') || code; // Formats to XXXX-XXXX
                
                console.log(`\n📲 YOUR PAIRING CODE IS: ${code}\n`);
                console.log(`1. Open WhatsApp on your phone.`);
                console.log(`2. Go to Settings > Linked Devices > Link a Device.`);
                console.log(`3. Tap "Link with phone number instead" at the bottom.`);
                console.log(`4. Enter the code!`);
            } catch (err) {
                console.error('\n❌ Failed to get pairing code:', err.message);
            }
        }

        if (connection === 'close') {
            const shouldReconnect = lastDisconnect.error?.output?.statusCode !== DisconnectReason.loggedOut;
            console.log('Connection closed. Reconnecting:', shouldReconnect);
            if (shouldReconnect) {
                // Wait 2 seconds before reconnecting to prevent infinite loops
                setTimeout(() => connectToWhatsApp(), 2000); 
            } else {
                console.log("Logged out permanently. Clear the 'whatsapp_session' table and restart.");
            }
        } else if (connection === 'open') {
            console.log('\n✅ RAM College WhatsApp Bot Successfully Linked & Active!\n');
        }
    });
}

// ---------------------------------------------------------
// EXPRESS API
// ---------------------------------------------------------
app.post('/api/send-whatsapp', async (req, res) => {
    const { phone, message } = req.body;
    if (!sock) return res.status(500).json({ success: false, error: "Not ready." });

    try {
        const jid = `${phone}@s.whatsapp.net`;
        await delay(2000);
        await sock.sendMessage(jid, { text: message });
        res.json({ success: true, message: "WhatsApp Delivered" });
    } catch (error) {
        res.status(500).json({ success: false, error: error.message });
    }
});

app.listen(3001, () => console.log(`🚀 Node.js Express listening on port 3001`));

// Boot the engine
connectToWhatsApp();