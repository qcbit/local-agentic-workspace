const esbuild = require('esbuild');

// 1. Check if the --watch flag was passed from the npm script
const isWatch = process.argv.includes('--watch');

// 2. Define your existing build configuration
const buildOptions = {
    entryPoints: {
        'webview': 'src/webview/index.tsx', // The Settings panel output (webview.js)
        'chat': 'src/webview/chat.tsx'      // The Chat panel output (chat.js)
    },
    bundle: true,
    outdir: 'out',
    format: 'iife',
    minify: true,
    sourcemap: true,
    define: {
        'process.env.NODE_ENV': '"production"' 
    }
};

async function build() {
    try {
        if (isWatch) {
            // 3. Use the Context API for watch mode
            // For development, you might want to dynamically swap NODE_ENV to '"development"' here
            // to get better React error messages, but we will leave it as production for now!
            const ctx = await esbuild.context(buildOptions);
            await ctx.watch();
            console.log('👀 Watching React webview files for changes...');
        } else {
            // Standard one-off build
            await esbuild.build(buildOptions);
            console.log('✅ Webview build complete.');
        }
    } catch (err) {
        console.error(err);
        process.exit(1);
    }
}

build();
