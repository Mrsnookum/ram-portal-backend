import { makeWASocket, DisconnectReason, initAuthCreds, proto, BufferJSON } from '@whiskeysockets/baileys';
import express from 'express';
import cors from 'cors';
import fs from 'fs';
import { createClient } from '@supabase/supabase-js';

// --- YOUR SUPABASE CREDENTIALS ---
const SUPABASE_URL = "https://atkcgxthfgpadgxgqeaj.supabase.co";
const SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImF0a2NneHRoZmdwYWRneGdxZWFqIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MjIwMjM2MiwiZXhwIjoyMDk3Nzc4MzYyfQ.t6oHYWOGiOagTkokdSgz5_Jn8R6P44Z5Tsp7IHvuHJ0";
const supabase = createClient(SUPABASE_URL, SUPABASE_KEY);

// --- FORCE CLEAN STATE ---
if (fs.existsSync('./auth_info_baileys')) {
    fs.rmSync('./auth_info_baileys', { recursive: true, force: true });
    console.log("🗑️ Cleared corrupted local session data.");
}

const app = express();
app.use(express.json());
app.use(cors());

app.get('/', (req, res) => {
    res.status(200).send('WhatsApp Engine is Awake!');
});

let sock = null;

const delay = ms => new Promise(res => setTimeout(res, ms));

// ==========================================
// ADMIN ALERT SYSTEM (TELEGRAM)
// ==========================================
async function notifyAdmin(message) {
    const botToken = process.env.TELEGRAM_BOT_TOKEN;
    const chatId = process.env.TELEGRAM_CHAT_ID;
    
    if (botToken && chatId) {
        try {
            await fetch(`https://api.telegram.org/bot${botToken}/sendMessage`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ 
                    chat_id: chatId, 
                    text: `🚨 *RAM Portal Alert*\n\n${message}`,
                    parse_mode: 'Markdown'
                })
            });
            console.log("📨 Telegram alert sent to admin.");
        } catch (error) {
            console.error("❌ Failed to send Telegram alert:", error);
        }
    }
}

// ==========================================
// THE SUPABASE CUSTOM AUTH ADAPTER
// ==========================================
async function useSupabaseAuthState() {
    const writeData = async (data, id) => {
        try {
            const serialized = JSON.parse(JSON.stringify(data, BufferJSON.replacer));
            await supabase.from('whatsapp_session').upsert({ id: id, data: serialized });
        } catch (error) {
            console.error("Error writing single key to Supabase:", error);
        }
    };

    const readData = async (id) => {
        try {
            const { data, error } = await supabase.from('whatsapp_session').select('data').eq('id', id).single();
            if (error || !data) return null;
            return JSON.parse(JSON.stringify(data.data), BufferJSON.reviver);
        } catch (error) {
            return null;
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
                    // FIX: BATCH writes. Sends all keys in ONE single lightning-fast request!
                    const upserts = [];
                    const deletes = [];
                    
                    for (const category in data) {
                        for (const id in data[category]) {
                            const value = data[category][id];
                            const key = `${category}-${id}`;
                            
                            if (value) {
                                const serialized = JSON.parse(JSON.stringify(value, BufferJSON.replacer));
                                upserts.push({ id: key, data: serialized });
                            } else {
                                deletes.push(key);
                            }
                        }
                    }
                    
                    try {
                        if (upserts.length > 0) {
                            await supabase.from('whatsapp_session').upsert(upserts);
                        }
                        if (deletes.length > 0) {
                            await supabase.from('whatsapp_session').delete().in('id', deletes);
                        }
                    } catch (error) {
                        console.error("❌ Batch DB write failed:", error);
                    }
                }
            }
        },
        saveCreds: () => writeData(creds, 'creds')
    };
}

async function connectToWhatsApp() {
    console.log("Starting WhatsApp Engine via Supabase...");
    
    const { state, saveCreds } = await useSupabaseAuthState();
    
    sock = makeWASocket({
        auth: state,
        printQRInTerminal: false
    });

    sock.ev.on('creds.update', saveCreds);

    sock.ev.on('connection.update', async (update) => {
        const { connection, lastDisconnect, qr } = update;
        
        if (qr && !sock.authState.creds.registered) {
            let rawNumber = process.env.BOT_PHONE_NUMBER || "254743611394";
            let cleanNumber = rawNumber.replace(/[^0-9]/g, ''); 
            
            try {
                let code = await sock.requestPairingCode(cleanNumber);
                code = code?.match(/.{1,4}/g)?.join('-') || code; 
                
                console.log(`\n=================================================`);
                console.log(`📲 YOUR PAIRING CODE IS: ${code}`);
                console.log(`=================================================\n`);
            } catch (err) {
                console.error('\n❌ Failed to get pairing code:', err.message);
            }
        }

        if (connection === 'close') {
            const shouldReconnect = lastDisconnect.error?.output?.statusCode !== DisconnectReason.loggedOut;
            console.log('Connection closed. Reconnecting:', shouldReconnect);
            
            if (shouldReconnect) {
                setTimeout(() => connectToWhatsApp(), 3000); 
            } else {
                console.log("Logged out permanently. Initiating auto-cleanup...");
                await notifyAdmin("WhatsApp Engine Disconnected! ⚠️\n\nThe device was logged out. The database is being auto-cleared. Please restart the Render server to generate a new pairing code.");
                
                try {
                    await supabase.from('whatsapp_session').delete().neq('id', '0'); 
                    console.log("✅ Database auto-cleared successfully.");
                } catch (e) {
                    console.error("❌ Failed to auto-clear database:", e);
                }
            }
        } else if (connection === 'open') {
            console.log('\n✅ RAM College WhatsApp Bot Successfully Linked & Active!\n');
            notifyAdmin("✅ WhatsApp Engine is connected and online!");
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

connectToWhatsApp();
