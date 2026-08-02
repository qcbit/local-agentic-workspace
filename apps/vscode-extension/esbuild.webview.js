const esbuild = require('esbuild');

esbuild.build({
    entryPoints: {
        'webview': 'src/webview/index.tsx', // The Settings panel output (webview.js)
        'chat': 'src/webview/chat.tsx'      // The Chat panel output (chat.js)
    },
    bundle: true,
    outdir: 'out',
    format: 'iife',     // Immediately Invoked Function Expression for browser
    minify: true,
    sourcemap: true,
}).catch(() => process.exit(1));
