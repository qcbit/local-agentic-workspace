import React, { useState, useEffect } from 'react';

// Safely acquire and cache the VS Code API
declare const acquireVsCodeApi: any;
const vscode = (window as any).vscodeApi || ((window as any).vscodeApi = acquireVsCodeApi());

export const SettingsPanel: React.FC = () => {
    // 1. Add a loading state, defaulting to true
    const [isLoading, setIsLoading] = useState(true);
    
    // Initial states no longer matter for the flicker, but we keep defaults just in case
    const [activeProfile, setActiveProfile] = useState('home');
    const [modelName, setModelName] = useState(''); 
    const [tokenLimit, setTokenLimit] = useState(6000);
    const [availableModels, setAvailableModels] = useState<string[]>([]);

    useEffect(() => {
        // Tell extension.ts we are ready to receive data
        vscode.postMessage({ command: 'ready' });

        const handleMessage = (event: MessageEvent) => {
            const message = event.data;
            
            if (message.command === 'loadModels') {
                setAvailableModels(message.models);
            } 
            else if (message.command === 'loadConfig') {
                const config = message.config;
                if (config) {
                    const currentProfile = config.active_profile || 'home';
                    setActiveProfile(currentProfile);
                    
                    const profileSettings = config.profiles?.[currentProfile];
                    if (profileSettings) {
                        if (profileSettings.llm?.model_name) {
                            setModelName(profileSettings.llm.model_name);
                        }
                        if (profileSettings.memory?.max_tokens) {
                            setTokenLimit(profileSettings.memory.max_tokens);
                        }
                    }
                }
                // 2. Mark loading as complete once the backend responds
                setIsLoading(false);
            }
        };

        window.addEventListener('message', handleMessage);
        return () => window.removeEventListener('message', handleMessage);
    }, []);

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

    // 3. Conditionally render a loading screen
    if (isLoading) {
        return (
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '100vh', gap: '1rem', color: 'var(--vscode-foreground)' }}>
                {/* A simple CSS spinner using VS Code theme variables */}
                <div style={{ 
                    border: '4px solid var(--vscode-widget-border)', 
                    borderTop: '4px solid var(--vscode-button-background)', 
                    borderRadius: '50%', 
                    width: '40px', 
                    height: '40px', 
                    animation: 'spin 1s linear infinite' 
                }}></div>
                <style>{`
                    @keyframes spin {
                        0% { transform: rotate(0deg); }
                        100% { transform: rotate(360deg); }
                    }
                `}</style>
                <h3>Fetching Workspace Environment...</h3>
            </div>
        );
    }

    // 4. Render the actual form once data is loaded
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
                <select 
                    value={modelName}
                    onChange={(e) => setModelName(e.target.value)}
                    style={{ padding: '0.5rem', marginTop: '0.25rem', backgroundColor: 'var(--vscode-input-background)', color: 'var(--vscode-input-foreground)', border: '1px solid var(--vscode-input-border)' }}
                >
                    {availableModels.length === 0 ? (
                         <option value={modelName}>{modelName}</option>
                    ) : (
                        availableModels.map(model => (
                            <option key={model} value={model}>{model}</option>
                        ))
                    )}
                </select>
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
