"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
const chai_1 = require("chai");
const ASTProvider_1 = require("../src/ast/ASTProvider");
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
// Fake parser library to avoid loading real WASM in unit tests
class FakeLanguage {
    name;
    constructor(name) {
        this.name = name;
    }
}
class FakeParserLib {
    static async init() {
        // no-op
    }
    static async Language_load(path) {
        return new FakeLanguage('fake');
    }
    static Language = {
        async load(path) {
            return new FakeLanguage('fake');
        }
    };
    constructor() {
        // instance methods used by ASTProvider
    }
    setLanguage(_lang) {
        // no-op
    }
    parse(text) {
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
        const extensionUri = { fsPath: '/tmp' };
        const provider = new ASTProvider_1.ASTProvider(extensionUri, FakeParserLib);
        // Ensure a fake WASM file exists so ASTProvider.loadLanguage's fs check passes
        const wasmDir = path.join(extensionUri.fsPath, 'wasm');
        fs.mkdirSync(wasmDir, { recursive: true });
        const wasmFile = path.join(wasmDir, 'tree-sitter-javascript.wasm');
        if (!fs.existsSync(wasmFile))
            fs.writeFileSync(wasmFile, '');
        // create a fake document
        const fakeDocument = {
            languageId: 'javascript',
            getText: () => 'function foo() { return 1; }\n',
            uri: { toString: () => 'file:///tmp/foo.js' }
        };
        const result = await provider.parseDocument(fakeDocument);
        (0, chai_1.expect)(result).to.have.property('symbols');
        (0, chai_1.expect)(result.symbols).to.be.an('array').that.is.not.empty;
        const firstSymbol = (result.symbols && result.symbols[0]) || '';
        (0, chai_1.expect)(firstSymbol).to.match(/function_declaration/);
    });
});
//# sourceMappingURL=test_ast.spec.js.map