const { Module } = require('../main');
const config = require('../config');

const getSnapshotEndpoint = (playUrl) => {
    const baseUrl = String(playUrl || '').trim().replace(/\/$/, '');
    if (!baseUrl) throw new Error('PLAY_URL is not configured');
    return baseUrl.includes('/api/video-to-photos')
        ? `${baseUrl}${baseUrl.includes('?') ? '&' : '?'}format=json`
        : `${baseUrl}/api/video-to-photos?format=json`;
};

Module({
    pattern: 'snapshot',
    desc: 'Extract clear UHD photos from a replied video',
    use: 'media',
    usage: 'snapshot',
}, async (message) => {
    const replied = message.reply_message;

    if (!replied || (!replied.video && !(replied.document && String(replied.mimetype || '').startsWith('video/')))) {
        return await message.sendReply('_Reply to a video with_ `.snapshot`');
    }

    try {
        const inputUrl = replied.url;
        if (!inputUrl) {
            return await message.sendReply('_I could not get the video URL._');
        }

        await message.sendReply('_Sending the video to TG_TAG for UHD snapshots..._');

        const endpoint = getSnapshotEndpoint(config.PLAY_URL);
        const response = await fetch(endpoint, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({
                url: inputUrl,
                format: 'json',
            }),
        });

        if (!response.ok) {
            throw new Error(`TG_TAG returned HTTP ${response.status}`);
        }

        const result = await response.json();
        if (result.type !== 'images' || !Array.isArray(result.images) || !result.images.length) {
            throw new Error('TG_TAG returned no snapshots');
        }

        for (let index = 0; index < result.images.length; index += 1) {
            const item = result.images[index];
            const imageBuffer = Buffer.from(item.data, 'base64');
            await message.sendMessage(imageBuffer, 'image', {
                fileName: item.filename || `snapshot-${index + 1}.jpg`,
                caption: index === 0 ? '_UHD snapshots from TG_TAG_' : undefined,
            });
        }
    } catch (error) {
        console.error('snapshot plugin error:', error);
        await message.sendReply('_TG_TAG could not extract photos from that video._');
    }
});
