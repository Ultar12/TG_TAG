const { Module } = require('../main');
const config = require('../config');
const fs = require('fs');
const { getTempPath } = require('../core/helpers');
const { addExif } = require('./utils');

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

Module({
    pattern: 'tg ?(.*)',
    desc: 'Download a Telegram sticker pack and send stickers as they finish processing',
    use: 'media',
    usage: 'tg <telegram sticker-pack URL>',
    warn: 'Send a Telegram sticker-pack URL, for example: `.tg https://t.me/addstickers/PackName`'
}, async (message, match) => {
    const packUrl = String(match[1] || '').trim();
    const serviceUrl = String(config.PLAY_URL || '').replace(/\/$/, '');

    if (!/^https?:\/\/t\.me\/addstickers\/[A-Za-z0-9_-]+(?:\?.*)?$/i.test(packUrl)) {
        return message.sendReply('_Send a valid Telegram sticker-pack URL._');
    }
    if (!serviceUrl) {
        return message.sendReply('_TG_TAG PLAY_URL is not configured._');
    }

    const progress = await message.sendReply('_Starting Telegram sticker processing..._');
    const sent = new Set();
    let tempCounter = 0;
    let lastSentAt = 0;

    const sendSticker = async sticker => {
        if (sent.has(sticker.index)) return;

        // Enforce five seconds between sends, without delaying the first one.
        const wait = 5000 - (Date.now() - lastSentAt);
        if (lastSentAt && wait > 0) await sleep(wait);

        const stickerResponse = await fetch(new URL(sticker.url, serviceUrl));
        if (!stickerResponse.ok) {
            throw new Error(`TG_TAG sticker ${sticker.index} returned HTTP ${stickerResponse.status}`);
        }

        const inputPath = getTempPath(`tg-sticker-${Date.now()}-${tempCounter++}.webp`);
        fs.writeFileSync(inputPath, Buffer.from(await stickerResponse.arrayBuffer()));
        let brandedPath = inputPath;
        try {
            // Some EXIF/WebP libraries flatten animations. Only brand static
            // stickers; animated stickers must use the original WebP bytes.
            if (!sticker.is_animated) {
                brandedPath = await addExif(inputPath, {
                    packname: 'Ultar Sync',
                    author: 'Ultar Sync',
                    categories: '⭐',
                    android: 'https://github.com/Ultar12/TG_TAG',
                    ios: 'https://github.com/Ultar12/TG_TAG'
                });
            }
            await message.sendMessage(fs.readFileSync(brandedPath), 'sticker');
            sent.add(sticker.index);
            lastSentAt = Date.now();
        } finally {
            try { fs.unlinkSync(inputPath); } catch {}
            if (brandedPath !== inputPath) {
                try { fs.unlinkSync(brandedPath); } catch {}
            }
        }
    };

    try {
        const response = await fetch(`${serviceUrl}/api/tg-stickers`, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ url: packUrl })
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(payload.error || `TG_TAG returned HTTP ${response.status}`);
        }

        const statusUrl = new URL(payload.status_url, serviceUrl);
        let status = payload;
        let lastProgress = '';

        while (true) {
            // Processing status includes every sticker completed so far.
            for (const sticker of (Array.isArray(status.stickers) ? status.stickers : [])) {
                await sendSticker(sticker);
            }

            if (status.state === 'failed') {
                throw new Error(status.error || 'TG_TAG could not process the sticker pack.');
            }
            if (status.state === 'ready') break;

            const currentProgress = `${sent.size}/${status.count || '?'} stickers sent`;
            if (currentProgress !== lastProgress) {
                lastProgress = currentProgress;
                await message.edit(`_Processing Telegram pack: ${currentProgress}..._`, message.jid, progress.key);
            }

            await sleep(2000);
            const statusResponse = await fetch(statusUrl);
            status = await statusResponse.json().catch(() => ({}));
            if (!statusResponse.ok) {
                throw new Error(status.error || `TG_TAG returned HTTP ${statusResponse.status}`);
            }
        }

        await message.edit(`_${sent.size} stickers sent with the Ultar Sync pack name._`, message.jid, progress.key);
    } catch (error) {
        await message.edit(`_Sticker pack failed after ${sent.size} stickers:_ ${error.message}`, message.jid, progress.key);
    }
});

module.exports = {};
