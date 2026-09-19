#!/usr/bin/env node
const Pornsearch = require('pornsearch');

const query = process.argv.slice(2).join(' ').trim();
const driver = process.env.PORNSEARCH_DRIVER || 'pornhub';

if (!query) {
    process.stdout.write(JSON.stringify({ ok: false, error: 'A search query is required.' }) + '\n');
    process.exit(2);
} else {
    (async () => {
        try {
            const searcher = new Pornsearch(query, driver);
            const results = await searcher.videos(1);
            const normalized = (Array.isArray(results) ? results : [])
                .filter((item) => item && typeof item.url === 'string' && /^https?:\/\//i.test(item.url))
                .slice(0, 10)
                .map((item) => ({
                    title: String(item.title || 'Untitled').replace(/\s+/g, ' ').trim(),
                    url: item.url,
                    duration: item.duration ? String(item.duration).trim() : '',
                }));
            process.stdout.write(JSON.stringify({ ok: true, driver, results: normalized }) + '\n');
            process.exit(0);
        } catch (error) {
            process.stdout.write(JSON.stringify({ ok: false, error: error.message || String(error) }) + '\n');
            process.exit(1);
        }
    })();
}
