import express = require('express');
import * as http from 'http';
import * as vscode from 'vscode';

export class WarpProxyServer {
    private app = express();
    private server: http.Server | null = null;
    private readonly port = 11435; 

    constructor() {
        this.setupRoutes();
    }

    private getActiveProfile() {
        const config = vscode.workspace.getConfiguration('localAgentic');
        const activeId = config.get<string>('activeProfile');
        const profiles = config.get<any[]>('profiles') || [];
        
        return profiles.find(p => p.id === activeId) || profiles[0];
    }

    private setupRoutes() {
        this.app.use(express.json());

        this.app.post('/v1/chat/completions', async (req, res) => {
            const profile = this.getActiveProfile();
            
            if (!profile) {
                res.status(500).json({ error: "No active AI profile configured." });
                return;
            }

            console.log(`🚀 Routing request to: ${profile.name} (${profile.endpointUrl})`);

            try {
                // Swap the model name in the payload to match the profile's model
                const payload = { ...req.body, model: req.body.model || profile.model };

                // Forward the request to the active LLM
                const targetUrl = `${profile.endpointUrl.replace(/\/$/, '')}${req.path}`;
                console.log(`🔍 DEBUG: Constructed targetUrl -> ${targetUrl}`);
                const response = await fetch(targetUrl, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': profile.apiKey === 'none' ? '' : `Bearer ${profile.apiKey}`
                    },
                    body: JSON.stringify(payload)
                });

                if (!response.ok) {
                    throw new Error(`LLM upstream error: ${response.statusText}`);
                }

                const data = await response.json();
                res.json(data);

            } catch (error: any) {
                console.error('Proxy Error:', error);
                res.status(500).json({
                    error: {
                        message: `Failed to route to ${profile.name}: ${error.message}`,
                        type: "proxy_routing_error"
                    }
                });
            }
        });
    }

    public start() {
        this.server = this.app.listen(this.port, '127.0.0.1', () => {
            console.log(`Local Agentic Proxy running on http://127.0.0.1:${this.port}`);
        });
    }

    public stop() {
        if (this.server) {
            this.server.close();
        }
    }
}
