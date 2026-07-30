import { expect } from 'chai';
import { ASTProvider } from '../src/ast/ASTProvider';
import * as fs from 'fs';
import * as path from 'path';

// Fake parser library to avoid loading real WASM in unit tests
class FakeLanguage {
    constructor(public name?: string) {}
}

class FakeParserLib {
    static async init() {
        // no-op
    }

    static async Language_load(path: string) {
        return new FakeLanguage('fake');
    }

    static Language = {
        async load(path: string) {
            return new FakeLanguage('fake');
        }
    };

    constructor() {
        // instance methods used by ASTProvider
    }

    setLanguage(_lang: any) {
        // no-op
    }

    parse(text: string) {
        return {
            rootNode: {
                type: 'module',
                text,
                children: [
                    {
                        type: 'function_declaration',
                        text: text.split('\n')[0] || text,
                        children: []
                    }
                ]
            }
        };
    }
}

describe('ASTProvider (unit)', () => {
    it('parses a simple document and extracts declarations', async () => {
        // Create a minimal extensionUri object with `fsPath` used by ASTProvider
        const extensionUri: any = { fsPath: '/tmp' };
        const provider = new ASTProvider(extensionUri as any, FakeParserLib as any);

        // Ensure a fake WASM file exists so ASTProvider.loadLanguage's fs check passes
        const wasmDir = path.join(extensionUri.fsPath, 'wasm');
        fs.mkdirSync(wasmDir, { recursive: true });
        const wasmFile = path.join(wasmDir, 'tree-sitter-javascript.wasm');
        if (!fs.existsSync(wasmFile)) fs.writeFileSync(wasmFile, '');

        // create a fake document
        const fakeDocument: any = {
            languageId: 'javascript',
            getText: () => 'function foo() { return 1; }\n',
            uri: { toString: () => 'file:///tmp/foo.js' }
        };

        const result = await provider.parseDocument(fakeDocument as any);
        expect(result).to.have.property('symbols');
        expect(result.symbols).to.be.an('array').that.is.not.empty;
        const firstSymbol = (result.symbols && result.symbols[0]) || '';
        expect(firstSymbol).to.match(/function_declaration/);
    });
});
