const esbuild = require('esbuild');

esbuild.build({
    entryPoints: {
        'webview': 'src/webview/index.tsx', // The Settings panel output (webview.js)
        'chat': 'src/webview/chat.tsx'      // The Chat panel output (chat.js)
    },
    bundle: true,
    outdir: 'out',
    format: 'iife',
    minify: true,
    sourcemap: true,
    // Add this define block to prevent React from crashing
    define: {
        'process.env.NODE_ENV': '"production"' 
    }
}).catch(() => process.exit(1));
