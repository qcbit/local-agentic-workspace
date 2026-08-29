import * as assert from 'assert';
import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import Mocha = require('mocha');

export function run(): Promise<void> {
    const mocha = new Mocha({
        ui: 'tdd',
        color: true
    });

    return new Promise((c, e) => {
        try {
            mocha.suite.emit('pre-require', global, '', mocha);

            suite('Extension Test Suite', () => {
                vscode.window.showInformationMessage('Start all tests.');

                test('Path expansion handles tildes correctly', () => {
                    const inputPath = '~/.ollama/models';
                    let expandedPath = inputPath;
                    
                    if (expandedPath.startsWith('~')) {
                        expandedPath = path.join(os.homedir(), expandedPath.slice(1));
                    }
                    
                    const expected = path.join(os.homedir(), '.ollama/models');
                    assert.strictEqual(expandedPath, expected);
                });

                test('Directory scanner filters out hidden files', () => {
                    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'agentic-test-'));
                    fs.writeFileSync(path.join(tempDir, 'llama3'), '');
                    fs.writeFileSync(path.join(tempDir, 'qwen2.5-coder'), '');
                    fs.writeFileSync(path.join(tempDir, '.DS_Store'), '');

                    const files = fs.readdirSync(tempDir).filter(f => !f.startsWith('.'));

                    assert.strictEqual(files.length, 2);
                    assert.ok(files.includes('llama3'));
                    assert.ok(files.includes('qwen2.5-coder'));
                    assert.ok(!files.includes('.DS_Store'));

                    fs.rmSync(tempDir, { recursive: true, force: true });
                });
            });

            // Explicitly type 'failures' as number
            mocha.run((failures: number) => {
                if (failures > 0) {
                    e(new Error(`Some tests failed. (${failures})`));
                } else {
                    c();
                }
            });
        } catch (err) {
            console.error(err);
            e(err);
        }
    });
}
