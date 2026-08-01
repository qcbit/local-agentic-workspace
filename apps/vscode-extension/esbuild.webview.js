const esbuild = require('esbuild');

esbuild.build({
    entryPoints: ['src/webview/index.tsx'],
    bundle: true,
    outfile: 'out/webview.js',
    format: 'iife',     // Immediately Invoked Function Expression for browser
    minify: true,
    sourcemap: true,
}).catch(() => process.exit(1));
