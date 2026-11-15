import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

function swPrecachePlugin() {
  let currentShellAssets = [];
  return {
    name: 'sw-precache-stamping',
    buildStart() {
      const assetsDir = path.resolve(__dirname, 'dist', 'assets');
      if (fs.existsSync(assetsDir)) {
        for (const f of fs.readdirSync(assetsDir)) {
          try {
            fs.unlinkSync(path.join(assetsDir, f));
          } catch {}
        }
      }
    },
    transformIndexHtml(html) {
      return html.replace(/ crossorigin/g, '');
    },
    generateBundle(options, bundle) {
      currentShellAssets = Object.keys(bundle).filter(
        name => (name.endsWith('.js') || name.endsWith('.css'))
          && !name.includes('TerminalPane')
          && !name.includes('CodePane'),
      );
    },
    closeBundle() {
      const dist = path.resolve(__dirname, 'dist');
      const swPath = path.join(dist, 'sw.js');
      if (!fs.existsSync(swPath)) return;

      const precacheUrls = [
        '/',
        '/manifest.webmanifest',
        '/rove-favicon.svg',
        '/rove-apple-touch.png',
        '/rove-192.png',
        ...currentShellAssets.map(f => `/watch/${f}`).sort(),
      ];

      const indexPath = path.join(dist, 'index.html');
      const indexSize = fs.existsSync(indexPath) ? fs.statSync(indexPath).size : 0;
      const digestSrc = `${precacheUrls.join('|')}|${indexSize}`;
      const buildHash = crypto.createHash('sha256').update(digestSrc).digest('hex').slice(0, 10);
      const cacheVersion = `walking-workstation-shell-${buildHash}`;

      let swText = fs.readFileSync(swPath, 'utf-8');
      swText = swText.replace(/__WORKSTATION_CACHE_VERSION__/g, cacheVersion);
      swText = swText.replace(
        /\/\*__WORKSTATION_PRECACHE_START__\*\/[\s\S]*?\/\*__WORKSTATION_PRECACHE_END__\*\//,
        JSON.stringify(precacheUrls, null, 2),
      );
      fs.writeFileSync(swPath, swText, 'utf-8');
      console.log(`[sw-precache] Stamped ${swPath} with ${cacheVersion} (${precacheUrls.length} precached shell assets)`);
    },
  };
}

export default defineConfig({
  plugins: [react(), swPrecachePlugin()],
  base: '/',
  experimental: {
    renderBuiltUrl(filename) {
      if (filename.startsWith('assets/')) {
        return `/watch/${filename}`;
      }
      return `/${filename}`;
    },
  },
  build: {
    emptyOutDir: false,
    chunkSizeWarningLimit: 600,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) return;
          if (
            id.includes('/react/') ||
            id.includes('/react-dom/') ||
            id.includes('/scheduler/')
          ) {
            return 'vendor-react';
          }
          if (
            id.includes('react-markdown') ||
            id.includes('remark-') ||
            id.includes('rehype-') ||
            id.includes('micromark') ||
            id.includes('mdast-') ||
            id.includes('hast-') ||
            id.includes('unist-') ||
            id.includes('vfile') ||
            id.includes('unified')
          ) {
            return 'vendor-markdown';
          }
          if (id.includes('lucide-react')) {
            return 'vendor-icons';
          }
        },
      },
    },
  },
});
