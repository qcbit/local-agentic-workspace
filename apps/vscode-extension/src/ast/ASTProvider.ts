import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';

// Destructure the named exports from the modern web-tree-sitter API
const { Parser, Language } = require('web-tree-sitter');

export class ASTProvider {
    private parser: any | null = null;
    private languageMap: Map<string, any> = new Map();
    private isInitialized = false;

    constructor(private readonly extensionUri: vscode.Uri) {}

    /**
     * Initializes the WebAssembly Tree-sitter environment.
     * Must be called before parsing any documents.
     */
    public async initialize(): Promise<void> {
        if (this.isInitialized) return;

        try {
            await Parser.init();
            this.parser = new Parser();
            this.isInitialized = true;
            console.log('Tree-sitter WASM initialized locally.');
        } catch (error) {
            console.error('Failed to initialize Tree-sitter:', error);
            throw error;
        }
    }

    // ... Keep your existing loadLanguage, parseDocument, and extractDeclarations methods exactly as they are below here!

    /**
     * Loads a specific language grammar (.wasm file) into memory.
     */
    private async loadLanguage(languageId: string): Promise<any | null> {
        if (this.languageMap.has(languageId)) {
            return this.languageMap.get(languageId)!;
        }

        // Map VS Code language IDs to compiled WASM binary names
        const wasmFiles: Record<string, string> = {
            'rust': 'tree-sitter-rust.wasm',
            'python': 'tree-sitter-python.wasm',
            'typescript': 'tree-sitter-typescript.wasm',
            'javascript': 'tree-sitter-javascript.wasm'
        };

        const wasmFileName = wasmFiles[languageId];
        if (!wasmFileName) {
            console.warn(`No AST parser configured for language: ${languageId}`);
            return null;
        }

        const wasmPath = path.join(this.extensionUri.fsPath, 'wasm', wasmFileName);
        
        if (!fs.existsSync(wasmPath)) {
            console.warn(`WASM file not found at path: ${wasmPath}`);
            return null;
        }

        try {
            const language = await Language.load(wasmPath);
            this.languageMap.set(languageId, language);
            return language;
        } catch (error) {
            console.error(`Error loading grammar for ${languageId}:`, error);
            return null;
        }
    }

    /**
     * Parses a VS Code TextDocument and returns a simplified symbol map
     * optimized for the local agent's context window.
     */
    public async parseDocument(document: vscode.TextDocument) {
        if (!this.parser || !this.isInitialized) {
            await this.initialize();
        }

        const language = await this.loadLanguage(document.languageId);
        if (!language) {
            return { error: 'Language not supported for AST parsing.' };
        }

        this.parser!.setLanguage(language);
        const tree = this.parser!.parse(document.getText());

        // Example: Extract high-level declarations to feed to the Agent
        const symbols = this.extractDeclarations(tree.rootNode);
        
        return {
            uri: document.uri.toString(),
            language: document.languageId,
            symbols: symbols
        };
    }

    /**
     * Recursively traverses the AST to extract function, struct, and class definitions.
     */
    private extractDeclarations(node: any): string[] {
        const declarations: string[] = [];
        
        // Define AST node types we want to capture as contextual anchors
        const targetTypes = [
            'function_item', 'struct_item', 'impl_item', // Rust
            'function_definition', 'class_definition',   // Python
            'function_declaration', 'class_declaration'  // TS/JS
        ];

        if (targetTypes.includes(node.type)) {
            // Extract the first line/signature of the declaration
            const signature = node.text.split('\n')[0];
            declarations.push(`[${node.type}] ${signature}`);
        }

        for (const child of node.children) {
            declarations.push(...this.extractDeclarations(child));
        }

        return declarations;
    }
}
