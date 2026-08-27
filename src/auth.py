"""Uygulama girişi (Streamlit native auth yok; session_state + form).

`.env` içinde `APP_AUTH_PASSWORD` doluysa login zorunludur. Kodda varsayılan boş
bırakılır; boşken giriş ekranı gösterilmez.
"""

from __future__ import annotations

import hmac
from typing import Optional

import streamlit as st

from .config import Settings, get_settings

SESSION_KEY = "authenticated"


def auth_is_required(settings: Optional[Settings] = None) -> bool:
    settings = settings or get_settings()
    return bool((settings.auth_password or "").strip())


def check_credentials(
    username: str,
    password: str,
    settings: Optional[Settings] = None,
) -> bool:
    """Kullanıcı adı ve şifreyi ayarlardaki değerlerle karşılaştırır."""
    settings = settings or get_settings()
    expected_user = settings.auth_user or ""
    expected_pass = settings.auth_password or ""
    if not expected_pass:
        return False
    user_ok = hmac.compare_digest(str(username or "").encode("utf-8"), expected_user.encode("utf-8"))
    pass_ok = hmac.compare_digest(str(password or "").encode("utf-8"), expected_pass.encode("utf-8"))
    return user_ok and pass_ok


def logout() -> None:
    st.session_state[SESSION_KEY] = False


def render_logout_button() -> None:
    if not auth_is_required():
        return
    st.divider()
    if st.button("Çıkış", width="stretch", help="Oturumu kapat"):
        logout()
        st.rerun()


def require_login() -> None:
    """Login zorunluysa formu gösterir ve sohbeti çizmez (`st.stop`)."""
    settings = get_settings()
    if not auth_is_required(settings):
        return
    if st.session_state.get(SESSION_KEY):
        return

    from . import ui

    ui.inject_css()
    st.markdown(
        "<style>section[data-testid='stSidebar']{display:none;}</style>",
        unsafe_allow_html=True,
    )
    st.markdown("<div class='login-shell'>", unsafe_allow_html=True)
    ui.header(settings.app_title, settings.app_subtitle)

    with st.form("login_form"):
        username = st.text_input("Kullanıcı adı", autocomplete="username")
        password = st.text_input("Şifre", type="password", autocomplete="current-password")
        submitted = st.form_submit_button("Giriş", width="stretch")

    if submitted:
        if check_credentials(username, password, settings):
            st.session_state[SESSION_KEY] = True
            st.rerun()
        st.error("Kullanıcı adı veya şifre hatalı.")

    st.markdown("</div>", unsafe_allow_html=True)
    st.stop()
