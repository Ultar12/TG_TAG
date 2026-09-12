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
    usage: 'snapshot'
}, async (message) => {
    const replied = message.reply_message;

    if (!replied || (!replied.video && !(replied.document && String(replied.mimetype || '').startsWith('video/')))) {
        return await message.sendReply('_Reply to a video with_ `.snapshot`');
    }

    try {
        const videoBuffer = await replied.download('buffer');
        if (!videoBuffer || !videoBuffer.length) {
            throw new Error('Could not download the replied video');
        }

        await message.sendReply('_Uploading the video to TG_TAG for UHD snapshots..._');

        const response = await fetch(getSnapshotEndpoint(config.PLAY_URL), {
            method: 'POST',
            headers: {
                'content-type': replied.mimetype || 'video/mp4'
            },
            body: videoBuffer
        });

        if (!response.ok) {
            const errorText = await response.text();
            throw new Error(`TG_TAG returned HTTP ${response.status}: ${errorText.slice(0, 200)}`);
        }

        const result = await response.json();
        if (result.type !== 'images' || !Array.isArray(result.images) || !result.images.length) {
            throw new Error('TG_TAG returned no snapshots');
        }

        for (let index = 0; index < result.images.length; index += 1) {
            const item = result.images[index];
            await message.sendMessage(Buffer.from(item.data, 'base64'), 'image', {
                fileName: item.filename || `snapshot-${index + 1}.jpg`,
                caption: index === 0 ? '_UHD snapshots from TG_TAG_' : undefined
            });
        }
    } catch (error) {
        console.error('snapshot plugin error:', error);
        await message.sendReply(`_TG_TAG could not extract photos from that video._\n\n\`${String(error.message || error).slice(0, 250)}\``);
    }
});
