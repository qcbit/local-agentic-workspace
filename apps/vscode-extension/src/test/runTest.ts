import * as path from 'path';
import * as os from 'os';
import * as fs from 'fs';
import { runTests } from '@vscode/test-electron';

async function main() {
    try {
        const extensionDevelopmentPath = path.resolve(__dirname, '../../');
        const extensionTestsPath = path.resolve(__dirname, './suite/extension.test');

        // Use a short path in the system temp directory to bypass socket length limits (<103 chars)
        const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'vsc-'));
        const userDataDir = path.join(tmpDir, 'u');
        const extensionsDir = path.join(tmpDir, 'e');

        await runTests({
            extensionDevelopmentPath,
            extensionTestsPath,
            launchArgs: [
                '--user-data-dir', userDataDir,
                '--extensions-dir', extensionsDir,
                '--no-sandbox',
                '--disable-gpu'
            ]
        });
    } catch (err) {
        console.error('Failed to run tests', err);
        process.exit(1);
    }
}

main();
