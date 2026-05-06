/**
 * auth.js — RennAuktion / RennMarkt shared Supabase auth layer
 * Handles: login modal, session persistence, save/unsave auctions & listings
 *
 * Auth methods: Google OAuth, email+password (sign in / sign up), magic link
 * Sessions persist via localStorage — users stay logged in across visits.
 */

const SUPABASE_URL      = 'https://kulgecvykrhfalvvyeru.supabase.co';
const SUPABASE_ANON_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imt1bGdlY3Z5a3JoZmFsdnZ5ZXJ1Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3Nzc2NDkyODcsImV4cCI6MjA5MzIyNTI4N30.xvylwBUU-Tt7-dbUUe68o2RZCIBtZrnspMconUFgqH4';

// ── Init Supabase client ──────────────────────────────────────────────────────
const _sb = supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

// ── State ─────────────────────────────────────────────────────────────────────
let _user = null;
let _savedIds = new Set();
const _TABLE = document.location.hostname.includes('rennauktion')
  ? 'saved_auctions'
  : 'saved_listings';

// ── Session ───────────────────────────────────────────────────────────────────
async function initAuth() {
  const { data: { session } } = await _sb.auth.getSession();
  if (session) {
    _user = session.user;
    await _loadSaved();
    _renderAuthState();
  }

  _sb.auth.onAuthStateChange(async (event, session) => {
    _user = session?.user ?? null;
    if (_user) {
      await _loadSaved();
      closeLoginModal();
    } else {
      _savedIds.clear();
    }
    _renderAuthState();
    _syncAllSaveButtons();
  });
}

async function _loadSaved() {
  if (!_user) return;
  const { data } = await _sb.from(_TABLE)
    .select('listing_id')
    .eq('user_id', _user.id);
  _savedIds = new Set((data || []).map(r => r.listing_id));
}

// ── Save / unsave ─────────────────────────────────────────────────────────────
async function toggleSave(listingId, meta = {}) {
  if (!_user) { openLoginModal(); return; }

  if (_savedIds.has(listingId)) {
    await _sb.from(_TABLE).delete()
      .eq('user_id', _user.id)
      .eq('listing_id', listingId);
    _savedIds.delete(listingId);
  } else {
    await _sb.from(_TABLE).insert({
      user_id:     _user.id,
      listing_id:  listingId,
      source:      meta.source || null,
      title:       meta.title  || null,
      listing_url: meta.url    || null,
      image_url:   meta.img    || null,
    });
    _savedIds.add(listingId);
  }
  _syncSaveButton(listingId);
}

function isSaved(listingId) {
  return _savedIds.has(listingId);
}

// ── UI helpers ────────────────────────────────────────────────────────────────
function _syncSaveButton(listingId) {
  document.querySelectorAll(`[data-save-id="${listingId}"]`).forEach(btn => {
    btn.classList.toggle('active', _savedIds.has(listingId));
  });
}

function _syncAllSaveButtons() {
  document.querySelectorAll('[data-save-id]').forEach(btn => {
    btn.classList.toggle('active', _savedIds.has(btn.dataset.saveId));
  });
}

function _renderAuthState() {
  const loginBtns  = document.querySelectorAll('.auth-login-btn');
  const logoutBtns = document.querySelectorAll('.auth-logout-btn');
  const userLabels = document.querySelectorAll('.auth-user-label');

  if (_user) {
    loginBtns.forEach(b  => b.style.display = 'none');
    logoutBtns.forEach(b => b.style.display = '');
    userLabels.forEach(el => el.textContent = _user.email?.split('@')[0] || 'Account');
  } else {
    loginBtns.forEach(b  => b.style.display = '');
    logoutBtns.forEach(b => b.style.display = 'none');
    userLabels.forEach(el => el.textContent = '');
  }
}

// ── Login modal ───────────────────────────────────────────────────────────────
function openLoginModal(defaultTab = 'signin') {
  let modal = document.getElementById('auth-modal');
  if (!modal) modal = _createLoginModal();
  modal.style.display = 'flex';
  _switchTab(defaultTab);
}

function closeLoginModal() {
  const modal = document.getElementById('auth-modal');
  if (modal) modal.style.display = 'none';
}

function _switchTab(tab) {
  ['signin','signup','magic'].forEach(t => {
    const el = document.getElementById(`auth-tab-${t}`);
    const pane = document.getElementById(`auth-pane-${t}`);
    if (el) el.style.borderBottom = t === tab ? '2px solid #c0392b' : '2px solid transparent';
    if (el) el.style.color = t === tab ? '#e2ddd8' : '#666';
    if (pane) pane.style.display = t === tab ? 'block' : 'none';
  });
  const msg = document.getElementById('auth-msg');
  if (msg) msg.textContent = '';
}

function _msg(text, isError = false) {
  const el = document.getElementById('auth-msg');
  if (el) { el.textContent = text; el.style.color = isError ? '#e74c3c' : '#888'; }
}

function _createLoginModal() {
  const modal = document.createElement('div');
  modal.id = 'auth-modal';
  modal.style.cssText = `
    display:none; position:fixed; inset:0; z-index:1000;
    background:rgba(0,0,0,0.75); align-items:center; justify-content:center;
  `;

  const inputStyle = `
    width:100%; padding:10px 12px; border-radius:8px; border:1px solid #2e2e2e;
    background:#0d0d0d; color:#e2ddd8; font-size:14px; box-sizing:border-box;
    margin-bottom:8px; outline:none;
  `;
  const pwWrap = `position:relative; margin-bottom:8px;`;
  const pwInput = `
    width:100%; padding:10px 38px 10px 12px; border-radius:8px; border:1px solid #2e2e2e;
    background:#0d0d0d; color:#e2ddd8; font-size:14px; box-sizing:border-box; outline:none;
  `;
  const pwEye = `
    position:absolute; right:10px; top:50%; transform:translateY(-50%);
    background:none; border:none; color:#555; cursor:pointer; font-size:16px; padding:0;
  `;
  const primaryBtn = `
    width:100%; padding:11px; border-radius:8px; border:none;
    background:#c0392b; color:#fff; font-size:14px; font-weight:600; cursor:pointer;
    margin-top:4px;
  `;
  const linkBtn = `
    background:none; border:none; color:#c0392b; font-size:12px;
    cursor:pointer; padding:0; text-decoration:underline;
  `;

  modal.innerHTML = `
    <div style="background:#141414; border:1px solid #2e2e2e; border-radius:12px;
                padding:32px; width:380px; max-width:92vw; position:relative;">
      <button onclick="closeLoginModal()" style="position:absolute;top:12px;right:14px;
        background:none;border:none;color:#555;font-size:22px;cursor:pointer;line-height:1;">×</button>

      <div style="font-family:'DM Mono',monospace;font-size:11px;letter-spacing:2px;
                  text-transform:uppercase;color:#c0392b;margin-bottom:4px;">RennAuktion</div>
      <h2 style="font-size:20px;font-weight:700;margin-bottom:20px;">Sign in to save</h2>

      <!-- Google -->
      <button id="auth-google-btn" style="
        width:100%; padding:11px; border-radius:8px; border:1px solid #333;
        background:#1c1c1c; color:#e2ddd8; font-size:14px; cursor:pointer;
        display:flex; align-items:center; justify-content:center; gap:10px; margin-bottom:16px;">
        <svg width="18" height="18" viewBox="0 0 24 24">
          <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
          <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
          <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l3.66-2.84z"/>
          <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/>
        </svg>
        Continue with Google
      </button>

      <div style="display:flex;align-items:center;gap:8px;margin-bottom:16px;">
        <div style="flex:1;height:1px;background:#2e2e2e;"></div>
        <span style="color:#444;font-size:11px;">or use email</span>
        <div style="flex:1;height:1px;background:#2e2e2e;"></div>
      </div>

      <!-- Tabs -->
      <div style="display:flex;gap:0;border-bottom:1px solid #2e2e2e;margin-bottom:16px;">
        <button id="auth-tab-signin" onclick="_switchTab('signin')" style="
          flex:1;background:none;border:none;border-bottom:2px solid #c0392b;
          color:#e2ddd8;font-size:13px;padding:6px 0;cursor:pointer;font-weight:600;">Sign In</button>
        <button id="auth-tab-signup" onclick="_switchTab('signup')" style="
          flex:1;background:none;border:none;border-bottom:2px solid transparent;
          color:#666;font-size:13px;padding:6px 0;cursor:pointer;">Create Account</button>
        <button id="auth-tab-magic" onclick="_switchTab('magic')" style="
          flex:1;background:none;border:none;border-bottom:2px solid transparent;
          color:#666;font-size:13px;padding:6px 0;cursor:pointer;">Magic Link</button>
      </div>

      <!-- Sign In pane -->
      <div id="auth-pane-signin">
        <input id="auth-si-email" type="email" placeholder="your@email.com" style="${inputStyle}">
        <div style="${pwWrap}">
          <input id="auth-si-pw" type="password" placeholder="Password" style="${pwInput}">
          <button onclick="_togglePw('auth-si-pw',this)" style="${pwEye}">👁</button>
        </div>
        <div style="text-align:right;margin-bottom:12px;">
          <button onclick="_switchTab('magic')" style="${linkBtn}">Forgot password?</button>
        </div>
        <button id="auth-signin-btn" style="${primaryBtn}">Sign In</button>
      </div>

      <!-- Sign Up pane -->
      <div id="auth-pane-signup" style="display:none;">
        <input id="auth-su-email" type="email" placeholder="your@email.com" style="${inputStyle}">
        <div style="${pwWrap}">
          <input id="auth-su-pw" type="password" placeholder="Password (min 6 chars)" style="${pwInput}">
          <button onclick="_togglePw('auth-su-pw',this)" style="${pwEye}">👁</button>
        </div>
        <button id="auth-signup-btn" style="${primaryBtn}">Create Account</button>
        <p style="font-size:11px;color:#555;margin-top:10px;text-align:center;">
          You'll get a confirmation email to verify your address.
        </p>
      </div>

      <!-- Magic Link pane -->
      <div id="auth-pane-magic" style="display:none;">
        <p style="font-size:13px;color:#888;margin-bottom:12px;">
          Enter your email and we'll send a one-click sign-in link. No password needed.
        </p>
        <input id="auth-ml-email" type="email" placeholder="your@email.com" style="${inputStyle}">
        <button id="auth-magic-btn" style="${primaryBtn}">Send magic link</button>
      </div>

      <div id="auth-msg" style="margin-top:12px;font-size:12px;color:#888;text-align:center;min-height:18px;"></div>
    </div>
  `;

  document.body.appendChild(modal);

  // Google OAuth
  document.getElementById('auth-google-btn').addEventListener('click', async () => {
    await _sb.auth.signInWithOAuth({ provider: 'google',
      options: { redirectTo: window.location.href } });
  });

  // Sign In
  document.getElementById('auth-signin-btn').addEventListener('click', async () => {
    const email = document.getElementById('auth-si-email').value.trim();
    const password = document.getElementById('auth-si-pw').value;
    if (!email || !password) { _msg('Please enter email and password.', true); return; }
    const btn = document.getElementById('auth-signin-btn');
    btn.textContent = 'Signing in…'; btn.disabled = true;
    const { error } = await _sb.auth.signInWithPassword({ email, password });
    btn.textContent = 'Sign In'; btn.disabled = false;
    if (error) _msg(error.message, true);
    // success → onAuthStateChange fires, modal closes automatically
  });

  // Sign Up
  document.getElementById('auth-signup-btn').addEventListener('click', async () => {
    const email = document.getElementById('auth-su-email').value.trim();
    const password = document.getElementById('auth-su-pw').value;
    if (!email || !password) { _msg('Please enter email and password.', true); return; }
    if (password.length < 6) { _msg('Password must be at least 6 characters.', true); return; }
    const btn = document.getElementById('auth-signup-btn');
    btn.textContent = 'Creating…'; btn.disabled = true;
    const { error } = await _sb.auth.signUp({ email, password,
      options: { emailRedirectTo: window.location.href } });
    btn.textContent = 'Create Account'; btn.disabled = false;
    if (error) _msg(error.message, true);
    else _msg('✓ Check your email to confirm your account.');
  });

  // Magic link
  document.getElementById('auth-magic-btn').addEventListener('click', async () => {
    const email = document.getElementById('auth-ml-email').value.trim();
    if (!email) return;
    const btn = document.getElementById('auth-magic-btn');
    btn.textContent = 'Sending…'; btn.disabled = true;
    const { error } = await _sb.auth.signInWithOtp({ email,
      options: { emailRedirectTo: window.location.href } });
    btn.textContent = 'Send magic link'; btn.disabled = false;
    _msg(error ? 'Error: ' + error.message : '✓ Check your email for the magic link.', !!error);
  });

  // Click outside to close
  modal.addEventListener('click', e => { if (e.target === modal) closeLoginModal(); });

  return modal;
}

// ── Password show/hide ────────────────────────────────────────────────────────
function _togglePw(inputId, btn) {
  const input = document.getElementById(inputId);
  if (!input) return;
  input.type = input.type === 'password' ? 'text' : 'password';
  btn.textContent = input.type === 'password' ? '👁' : '🙈';
}

// ── Logout ────────────────────────────────────────────────────────────────────
async function signOut() {
  await _sb.auth.signOut();
}

// ── Auto-init on DOM ready ────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', initAuth);

// ── Exports (global) ─────────────────────────────────────────────────────────
window.RA = { toggleSave, isSaved, openLoginModal, closeLoginModal, signOut };
