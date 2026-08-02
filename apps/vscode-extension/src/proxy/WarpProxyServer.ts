import express = require('express');
import * as http from 'http';
import * as https from 'https';
import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';

export class WarpProxyServer {
    private app = express();
    private server: http.Server | null = null;
    private readonly port = 11435; 

    constructor() {
        this.setupRoutes();
    }

    private getActiveProfile() {
        try {
            const workspaceFolders = vscode.workspace.workspaceFolders;
            if (!workspaceFolders) { return this.getDefaultProfile(); }
            
            const configPath = path.join(workspaceFolders[0].uri.fsPath, 'services', 'orchestrator', 'config.json');
            
            // Fallback to defaults if the file doesn't exist yet
            if (!fs.existsSync(configPath)) { return this.getDefaultProfile(); }

            const configData = JSON.parse(fs.readFileSync(configPath, 'utf8'));
            const activeId = configData.active_profile || 'home';
            const profileSettings = configData.profiles?.[activeId];
            
            if (!profileSettings) { return this.getDefaultProfile(); }

            const rawEndpoint = profileSettings.llm?.endpoint_url || 'http://127.0.0.1:11434/v1/chat/completions';

            return {
                name: activeId,
                model: profileSettings.llm?.model_name || 'llama3:8b',
                endpointUrl: rawEndpoint.includes('11435') 
                    ? 'http://127.0.0.1:11434/v1/chat/completions' 
                    : rawEndpoint,
                apiKey: 'none'
            };
        } catch (error) {
            console.error("Failed to read orchestrator config:", error);
            return this.getDefaultProfile();
        }
    }

    private getDefaultProfile() {
        return {
            name: 'default',
            model: 'llama3:8b',
            endpointUrl: 'http://127.0.0.1:11434/v1/chat/completions',
            apiKey: 'none'
        };
    }

    private setupRoutes() {
        // Increase the limit to 50mb to handle massive LLM context windows
        this.app.use(express.json({ limit: '50mb' }));

        this.app.post('/v1/chat/completions', (req, res) => {
            const profile = this.getActiveProfile();
            
            console.log(`🚀 Routing request to: ${profile.name} (${profile.endpointUrl})`);

            try {
                const payload = { ...req.body, model: req.body.model || profile.model };
                const targetUrl = `${profile.endpointUrl.replace(/\/$/, '')}`;
                
                const targetUrlObj = new URL(targetUrl);
                const requestModule = targetUrlObj.protocol === 'https:' ? https : http;

                // Stringify the payload and calculate exact byte length to prevent chunked-encoding drops
                const payloadString = JSON.stringify(payload);
                const payloadBuffer = Buffer.from(payloadString, 'utf-8');

                const proxyReq = requestModule.request(targetUrl, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Content-Length': payloadBuffer.length,
                        'Authorization': profile.apiKey === 'none' ? '' : `Bearer ${profile.apiKey}`
                    }
                }, (proxyRes) => {
                    res.status(proxyRes.statusCode || 200);
                    Object.keys(proxyRes.headers).forEach(key => {
                        const headerVal = proxyRes.headers[key];
                        if (headerVal) {
                            res.setHeader(key, headerVal);
                        }
                    });
                    
                    proxyRes.pipe(res);
                });

                proxyReq.on('error', (error) => {
                    console.error('Proxy Upstream Error:', error);
                    if (!res.headersSent) {
                        res.status(500).json({
                            error: {
                                message: `Failed to route to ${profile.name}: ${error.message}`,
                                type: "proxy_routing_error"
                            }
                        });
                    }
                });

                proxyReq.write(payloadBuffer);
                proxyReq.end();

            } catch (error: any) {
                console.error('Proxy Setup Error:', error);
                res.status(500).json({
                    error: {
                        message: `Failed to construct request: ${error.message}`,
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
