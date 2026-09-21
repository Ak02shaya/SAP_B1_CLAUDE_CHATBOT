const API_BASE_URL = "";
let authToken = localStorage.getItem("sap_token") || null;
let conversationHistory = []; // Stores context for the AI memory
let currentAbortController = null; // Controls the Stop functionality

// DOM Elements
const loginContainer = document.getElementById("login-container");
const appContainer = document.getElementById("app-container");
const loginForm = document.getElementById("login-form");
const logoutBtn = document.getElementById("logout-btn");
const loginError = document.getElementById("login-error");
const chatForm = document.getElementById("chat-form");
const chatHistory = document.getElementById("chat-history");
const userInput = document.getElementById("user-input");
const stopBtn = document.getElementById("stop-btn"); // Stop Button reference

function showApp() {
    loginContainer.classList.add("hidden");
    appContainer.classList.remove("hidden");
    userInput.focus();
}

function showLogin() {
    appContainer.classList.add("hidden");
    loginContainer.classList.remove("hidden");
    authToken = null;
    conversationHistory = []; // Clear memory on logout
    localStorage.removeItem("sap_token");
}

function appendMessage(sender, text, isHtml = false) {
    const message = document.createElement("div");
    const content = document.createElement("div");
    const messageId = `message-${Date.now()}-${Math.random().toString(36).slice(2)}`;

    message.id = messageId;
    message.className = `message ${sender === "user" ? "user-message" : "ai-message"}`;
    content.className = "message-content";
    
    if (isHtml) {
        content.innerHTML = text;
    } else {
        content.textContent = text;
    }
    
    message.appendChild(content);
    chatHistory.appendChild(message);
    chatHistory.scrollTop = chatHistory.scrollHeight;

    return messageId;
}

function formatAIResponse(result) {
    const analysis = result.analysis || "No analysis was returned.";
    const evaluation = result.solution_evaluation || "";
    
    // Helper function to clean text: converts \n to <br> and **text** to bold HTML
    const cleanText = (text) => {
        return text
            .replace(/\\n/g, "<br>")          // Converts literal escaped "\n" to breaks
            .replace(/\n/g, "<br>")           // Converts standard newlines to breaks
            .replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>"); // Converts **text** to bold
    };

    let htmlOutput = cleanText(analysis);

    if (evaluation) {
        htmlOutput += `<br><br><strong>Note:</strong> ${cleanText(evaluation)}`;
    }
    
    return htmlOutput;
}

// Initialization: Check if user is already logged in
if (authToken) {
    showApp();
}

// ==========================================
// AUTHENTICATION LOGIC
// ==========================================
loginForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    loginError.textContent = "";
    
    const username = document.getElementById("username").value;
    const password = document.getElementById("password").value;
    const submitBtn = loginForm.querySelector("button");
    
    submitBtn.textContent = "Signing In...";
    submitBtn.disabled = true;

    try {
        const response = await fetch(`${API_BASE_URL}/api/auth/login`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username, password })
        });
        
        if (response.ok) {
            const data = await response.json();
            authToken = data.access_token;
            localStorage.setItem("sap_token", authToken);
            showApp();
        } else {
            const err = await response.json();
            loginError.textContent = "Login failed: " + (err.detail || "Invalid credentials");
        }
    } catch (error) {
        console.error("Login error:", error);
        loginError.textContent = "Cannot connect to server.";
    } finally {
        submitBtn.textContent = "Sign In";
        submitBtn.disabled = false;
    }
});

logoutBtn.addEventListener("click", async () => {
    try {
        await fetch(`${API_BASE_URL}/api/auth/logout`, {
            method: "POST",
            headers: { "Authorization": `Bearer ${authToken}` }
        });
    } catch (e) {
        console.error(e);
    }
    showLogin();
});

// ==========================================
// STOP BUTTON LISTENER
// ==========================================
if (stopBtn) {
    stopBtn.addEventListener("click", () => {
        if (currentAbortController) {
            currentAbortController.abort(); // Triggers the AbortError in fetch
        }
    });
}

// ==========================================
// CHAT LOGIC
// ==========================================
chatForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const question = userInput.value.trim();
    if (!question) return;

    const btnSend = chatForm.querySelector(".btn-send");

    // 1. Append User Message
    appendMessage("user", question);
    userInput.value = "";
    
    // Lock inputs & Swap buttons
    userInput.disabled = true;
    btnSend.classList.add("hidden"); // Hide Send button
    if (stopBtn) stopBtn.classList.remove("hidden"); // Show Stop button

    // 2. Append AI Thinking Bubble
    const aiBubbleId = appendMessage("ai", "Thinking... Querying live database...");
    
    // Initialize the AbortController for this exact request
    currentAbortController = new AbortController();

    try {
        const response = await fetch(`${API_BASE_URL}/api/chat`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "Authorization": `Bearer ${authToken}`
            },
            body: JSON.stringify({ 
                question: question,
                conversation_history: conversationHistory 
            }),
            signal: currentAbortController.signal // Links the fetch to the Stop button
        });

        const aiBubbleElement = document.getElementById(aiBubbleId);
        const contentContainer = aiBubbleElement.querySelector(".message-content");

        if (response.ok) {
            const result = await response.json();
            
            // Update conversational memory (store last 6 messages max)
            conversationHistory.push({ role: "user", content: question });
            conversationHistory.push({ role: "assistant", content: result.analysis });
            if (conversationHistory.length > 6) {
                conversationHistory = conversationHistory.slice(-6);
            }

            // Build HTML Response (Text Analysis)
            let finalHtml = `<div class="ai-text">${formatAIResponse(result)}</div>`;

            // --- INJECT DYNAMIC ACTION CHIPS ---
            let actionsHTML = `<div class="action-chips-container" style="display:flex; gap:8px; flex-wrap:wrap; margin-top:15px; padding-top:10px; border-top:1px solid #eee;">`;
            
            // Suggestion Chips
            const suggestions = Array.isArray(result.suggestions) ? result.suggestions : [];
            if (suggestions.length > 0) {
                suggestions.forEach((sugg) => {
                    actionsHTML += `<button type="button" onclick="triggerQuickAction('${sugg.replace(/'/g, "\\'")}')" style="background:#f0f4f8; color:#0056b3; border:1px solid #cce5ff; padding:6px 14px; border-radius:16px; font-size:13px; cursor:pointer; transition: 0.2s;">${sugg}</button>`;
                });
            }

            actionsHTML += `</div>`;
            finalHtml += actionsHTML;
            // -----------------------------------

            // Append Execution Time Badge
            if (result.execution_time_ms !== undefined) {
                finalHtml += `<div class="exec-time" style="margin-top:10px; color:#6b7280; font-size:0.85em;">
                                ⏱️ Database Execution: <strong>${result.execution_time_ms} ms</strong>
                              </div>`;
            }

            // Inject HTML
            contentContainer.innerHTML = finalHtml;
            chatHistory.scrollTop = chatHistory.scrollHeight;

        } else {
            const err = await response.json();
            if (response.status === 401) {
                alert("Session expired. Please sign in again.");
                showLogin();
            } else {
                contentContainer.textContent = "Error: " + (err.detail || "Server error");
            }
        }
    } catch (error) {
        console.error("Chat request failed:", error);
        const aiBubbleElement = document.getElementById(aiBubbleId);
        
        // Handle Abort explicitly or standard network errors
        if (error.name === "AbortError") {
            aiBubbleElement.querySelector(".message-content").innerHTML = "🛑 <em>Query generation stopped.</em>";
        } else if (error.name === "SyntaxError") {
            aiBubbleElement.querySelector(".message-content").textContent = "Server crashed or returned an invalid response.";
        } else {
            aiBubbleElement.querySelector(".message-content").textContent = `Connection failed: ${error.message}`;
        }
    } finally {
        // Reset Inputs and Buttons
        userInput.disabled = false;
        btnSend.classList.remove("hidden"); // Show Send
        if (stopBtn) stopBtn.classList.add("hidden"); // Hide Stop
        userInput.focus();
        currentAbortController = null; // Clear controller
    }
});

// ==========================================
// GLOBAL HELPER FUNCTIONS (Actions)
// ==========================================
window.triggerQuickAction = function(text) {
    userInput.value = text;
    // Dispatch a submit event to automatically send the message
    chatForm.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
};