#!/usr/bin/env node
const Tiktok = require('@tobyg74/tiktok-api-dl');

const url = process.argv[2];
if (!url) {
    process.stdout.write(JSON.stringify({ ok: false, error: 'A TikTok URL is required.' }) + '\n');
    process.exitCode = 2;
} else {
    (async () => {
        try {
            const response = await Tiktok.Downloader(url, {
                version: process.env.TIKTOK_API_DL_VERSION || 'v1',
                cookie: process.env.TIKTOK_COOKIE || undefined,
                proxy: process.env.TIKTOK_PROXY || undefined,
            });
            if (!response || response.status !== 'success' || !response.result) {
                throw new Error(response?.message || 'TikTok API returned no usable result.');
            }
            const result = response.result;
            if (result.type === 'image') {
                const slides = Array.isArray(result.images) ? result.images.filter(Boolean) : [];
                if (!slides.length) throw new Error('TikTok image post contained no downloadable images.');
                process.stdout.write(JSON.stringify({
                    ok: true,
                    result: { type: 'slides', slides, desc: result.desc || '' },
                }) + '\n');
                return;
            }
            const videoUrl = result.video?.downloadAddr?.[0]
                || result.video?.playAddr?.[0]
                || result.direct
                || result.videoHD
                || result.videoWatermark;
            if (!videoUrl) throw new Error('TikTok response contained no downloadable video URL.');
            process.stdout.write(JSON.stringify({
                ok: true,
                result: { type: 'video', url: videoUrl, desc: result.desc || '' },
            }) + '\n');
        } catch (error) {
            process.stdout.write(JSON.stringify({ ok: false, error: error.message || String(error) }) + '\n');
            process.exitCode = 1;
        }
    })();
}
