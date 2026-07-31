import express = require('express');
import * as http from 'http';
import * as vscode from 'vscode';

export class WarpProxyServer {
    private app = express();
    private server: http.Server | null = null;
    
    // We will use 11435 to sit right next to Ollama's default 11434
    private readonly port = 11435; 

    constructor() {
        this.setupRoutes();
    }

    private setupRoutes() {
        // Warp sends JSON payloads, so we must parse them
        this.app.use(express.json());

        // Intercept OpenAI-style chat completion requests
        this.app.post('/v1/chat/completions', (req, res) => {
            console.log('🚀 Intercepted Warp Request:', JSON.stringify(req.body, null, 2));

            // Mock OpenAI-compatible response to prove the intercept works
            res.json({
                id: 'chatcmpl-mock',
                object: 'chat.completion',
                created: Date.now(),
                model: 'local-agentic-proxy',
                choices: [{
                    index: 0,
                    message: {
                        role: 'assistant',
                        content: 'Proxy intercept successful! I am ready to process this terminal command.'
                    },
                    finish_reason: 'stop'
                }]
            });
        });
    }

    public start() {
        this.server = this.app.listen(this.port, () => {
            console.log(`Local Agentic Proxy running on http://localhost:${this.port}`);
            vscode.window.showInformationMessage(`Warp Proxy started on port ${this.port}`);
        });
    }

    public stop() {
        if (this.server) {
            this.server.close();
            console.log('Local Agentic Proxy stopped.');
        }
    }
}
