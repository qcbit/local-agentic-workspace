import React, { useState } from 'react';

declare const acquireVsCodeApi: any;
const vscode = (window as any).vscodeApi || ((window as any).vscodeApi = acquireVsCodeApi());

export const SettingsPanel: React.FC = () => {
    const [activeProfile, setActiveProfile] = useState('home');
    const [modelName, setModelName] = useState('llama3:8b');
    const [tokenLimit, setTokenLimit] = useState(6000);

    const handleSync = () => {
        vscode.postMessage({
            command: 'updateSetting',
            config: {
                active_profile: activeProfile,
                profile_settings: {
                    llm: { model_name: modelName },
                    memory: { max_tokens: tokenLimit }
                }
            }
        });
    };

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem', padding: '1rem' }}>
            <h2>Workspace Environment</h2>
            
            <div style={{ display: 'flex', flexDirection: 'column' }}>
                <label>Active Profile</label>
                <select 
                    value={activeProfile} 
                    onChange={(e) => setActiveProfile(e.target.value)}
                    style={{ padding: '0.5rem', marginTop: '0.25rem', backgroundColor: 'var(--vscode-input-background)', color: 'var(--vscode-input-foreground)', border: '1px solid var(--vscode-input-border)' }}
                >
                    <option value="home">🏠 Home</option>
                    <option value="work">🏢 Work</option>
                </select>
            </div>

            <hr style={{ width: '100%', borderColor: 'var(--vscode-widget-border)' }} />
            
            <h3>{activeProfile.toUpperCase()} Settings</h3>

            <div style={{ display: 'flex', flexDirection: 'column' }}>
                <label>Model Name</label>
                <input 
                    type="text" 
                    value={modelName}
                    onChange={(e) => setModelName(e.target.value)}
                    style={{ padding: '0.5rem', marginTop: '0.25rem', backgroundColor: 'var(--vscode-input-background)', color: 'var(--vscode-input-foreground)', border: '1px solid var(--vscode-input-border)' }}
                />
            </div>

            <div style={{ display: 'flex', flexDirection: 'column' }}>
                <label>Max Context Tokens</label>
                <input 
                    type="number" 
                    value={tokenLimit}
                    onChange={(e) => setTokenLimit(Number(e.target.value))}
                    style={{ padding: '0.5rem', marginTop: '0.25rem', backgroundColor: 'var(--vscode-input-background)', color: 'var(--vscode-input-foreground)', border: '1px solid var(--vscode-input-border)' }}
                />
            </div>

            <button 
                onClick={handleSync}
                style={{ padding: '0.5rem 1rem', cursor: 'pointer', marginTop: '1rem', backgroundColor: 'var(--vscode-button-background)', color: 'var(--vscode-button-foreground)', border: 'none' }}
            >
                Sync to Orchestrator
            </button>
        </div>
    );
};
