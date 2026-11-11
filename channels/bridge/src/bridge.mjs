import readline from 'node:readline';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import makeWASocket, {
  Browsers,
  DisconnectReason,
  downloadMediaMessage,
  useMultiFileAuthState,
} from '@whiskeysockets/baileys';
import pino from 'pino';
import qrcode from 'qrcode-terminal';

const here = path.dirname(fileURLToPath(import.meta.url));
const authDir = path.resolve(here, '..', 'auth_info_baileys');
const maxAudioBytes = 16 * 1024 * 1024;
const logger = pino({ level: 'silent' });
const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
const browser = process.platform === 'win32'
  ? Browsers.windows('jarvS')
  : process.platform === 'darwin'
    ? Browsers.macOS('jarvS')
    : Browsers.ubuntu('jarvS');
let socket;
let reconnectTimer;
let shuttingDown = false;

function emit(event) {
  process.stdout.write(`${JSON.stringify(event)}\n`);
}

function messageText(message) {
  return message.conversation
    || message.extendedTextMessage?.text
    || message.imageMessage?.caption
    || message.videoMessage?.caption
    || message.documentMessage?.caption
    || message.buttonsResponseMessage?.selectedDisplayText
    || message.listResponseMessage?.title
    || '';
}

async function connect() {
  const { state, saveCreds } = await useMultiFileAuthState(authDir);
  const current = makeWASocket({
    auth: state,
    logger,
    browser,
    markOnlineOnConnect: false,
    syncFullHistory: false,
    generateHighQualityLinkPreview: false,
  });
  socket = current;
  current.ev.on('creds.update', saveCreds);

  current.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;
    if (qr) {
      qrcode.generate(qr, { small: true }, (code) => {
        process.stderr.write(`\nScan this WhatsApp QR with your linked device:\n${code}\n`);
      });
      emit({ type: 'connection', state: 'qr_ready' });
    }
    if (connection) emit({ type: 'connection', state: connection });
    if (connection === 'close' && !shuttingDown) {
      const status = lastDisconnect?.error?.output?.statusCode;
      if (status === DisconnectReason.loggedOut) {
        emit({ type: 'bridge_error', message: 'WhatsApp session logged out; remove local auth and pair again.' });
        return;
      }
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(() => {
        connect().catch((error) => emit({ type: 'bridge_error', message: String(error) }));
      }, 3000);
    }
  });

  current.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return;
    for (const item of messages || []) {
      const jid = item.key?.remoteJid || '';
      const message = item.message;
      if (!jid || jid === 'status@broadcast' || jid.endsWith('@g.us') || item.key?.fromMe || !message) continue;
      const audio = message.audioMessage;
      const unsupportedMedia = Boolean(
        message.imageMessage || message.videoMessage || message.documentMessage
          || message.stickerMessage || message.locationMessage || message.contactMessage,
      );
      const text = messageText(message);
      const event = {
        type: 'message',
        jid,
        from_me: false,
        text,
        push_name: item.pushName || '',
        unsupported_media: unsupportedMedia,
      };
      if (audio) {
        const size = Number(audio.fileLength || 0);
        if (size > maxAudioBytes) {
          event.unsupported_media = true;
          event.text = '';
        } else {
          try {
            const buffer = await downloadMediaMessage(item, 'buffer', {}, { logger });
            if (buffer.length <= maxAudioBytes) {
              event.audio_b64 = buffer.toString('base64');
              event.mimetype = audio.mimetype || 'audio/ogg';
            } else {
              event.unsupported_media = true;
            }
          } catch (error) {
            emit({ type: 'bridge_error', message: `voice download failed: ${String(error)}` });
            continue;
          }
        }
      }
      emit(event);
    }
  });

}

input.on('line', async (line) => {
  let command;
  try {
    command = JSON.parse(line);
  } catch {
    emit({ type: 'bridge_error', message: 'invalid command JSON' });
    return;
  }
  if (command.type === 'shutdown') {
    shuttingDown = true;
    clearTimeout(reconnectTimer);
    await socket?.end(new Error('jarvS channel shutdown'));
    process.exit(0);
  }
  if (command.type !== 'send_text') return;
  try {
    if (!socket?.user) throw new Error('WhatsApp is not connected');
    if (typeof command.jid !== 'string' || typeof command.text !== 'string') {
      throw new Error('send_text requires jid and text');
    }
    await socket.sendMessage(command.jid, { text: command.text.slice(0, 4000) });
    emit({ type: 'send_result', request_id: command.request_id, ok: true });
  } catch (error) {
    emit({ type: 'send_result', request_id: command.request_id, ok: false, error: String(error) });
  }
});

connect().catch((error) => {
  emit({ type: 'bridge_error', message: String(error) });
  process.exitCode = 1;
});
