// Authentication Module
// Password validation via Convex backend

const AUTH_SESSION_KEY = 'ieee_presence_authenticated';

// Check if already authenticated on page load
(function checkAuth() {
    if (sessionStorage.getItem(AUTH_SESSION_KEY) === 'true') {
        showMainApp();
    }
})();

async function handleAuth(event) {
    event.preventDefault();

    const passwordInput = document.getElementById('auth-password');
    const errorDiv = document.getElementById('auth-error');
    const submitBtn = document.querySelector('.auth-submit');
    const password = passwordInput.value;

    if (!password.trim()) {
        errorDiv.textContent = 'Please enter a password';
        passwordInput.focus();
        return false;
    }

    // Disable button and show loading state
    submitBtn.disabled = true;
    submitBtn.textContent = 'Verifying...';
    errorDiv.textContent = '';
    passwordInput.classList.remove('error');

    try {
        // Validate password via Convex backend
        const result = await window.convexClient.query("auth:validatePassword", { password });

        if (result.success) {
            // Store authentication in session
            sessionStorage.setItem(AUTH_SESSION_KEY, 'true');

            // Animate transition
            const overlay = document.getElementById('auth-overlay');
            overlay.classList.add('fade-out');

            // After animation, hide completely and show app
            setTimeout(() => {
                overlay.classList.add('hidden');
                showMainApp();
            }, 300);
        } else {
            // Show error
            errorDiv.textContent = result.error || 'Incorrect password';
            passwordInput.classList.add('error');
            passwordInput.value = '';
            passwordInput.focus();

            // Shake animation
            const container = document.querySelector('.auth-container');
            container.classList.add('shake');
            setTimeout(() => {
                container.classList.remove('shake');
            }, 500);

            // Re-enable button
            submitBtn.disabled = false;
            submitBtn.textContent = 'Unlock';
        }
    } catch (err) {
        console.error('Auth error:', err);
        errorDiv.textContent = 'Authentication failed. Please try again.';

        // Re-enable button
        submitBtn.disabled = false;
        submitBtn.textContent = 'Unlock';
    }

    return false;
}

function showMainApp() {
    const overlay = document.getElementById('auth-overlay');
    const mainApp = document.getElementById('main-app');

    // Hide the overlay completely
    overlay.classList.add('fade-out');
    overlay.classList.add('hidden');

    // Show and animate main app
    mainApp.style.display = 'block';
    // Force reflow to trigger animation
    void mainApp.offsetWidth;
    mainApp.classList.add('fade-in');

    // Initialize the app (start Convex subscription)
    if (typeof window.initializeApp === 'function') {
        window.initializeApp();
    }
}

// Make handleAuth available globally
window.handleAuth = handleAuth;
