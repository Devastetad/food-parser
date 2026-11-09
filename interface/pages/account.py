"""Аккаунт: почта, тариф, платежи, смена пароля, выход."""
import streamlit as st

from core.accounts import PLANS, AccountError
from interface.components.auth import current_user, sign_out, store
from interface.components.shared import flash, show_flash

STATUSES = {"pending": "Ожидает оплаты", "paid": "Оплачен", "canceled": "Отменён", "refunded": "Возвращён"}

show_flash()
st.title("Аккаунт")
user = store().get_user(current_user().id)
st.write(f"Почта: **{user.email}**")
if user.is_pro:
    st.write(f"Тариф: **{PLANS[user.plan].title}**, действует до {user.plan_until[:10]}")
else:
    st.write("Тариф: **Бесплатный**")
    st.caption("Платные тарифы появятся позже.")

payments = store().payments_of(user.id)
if payments:
    st.subheader("Платежи")
    st.dataframe(
        [
            {
                "Дата": created[:10],
                "Тариф": PLANS[plan].title if plan in PLANS else plan,
                "Сумма, ₽": amount / 100,
                "Статус": STATUSES.get(status, status),
            }
            for _, plan, amount, status, created, _ in payments
        ],
        hide_index=True,
    )

st.subheader("Смена пароля")
with st.form("change_password", clear_on_submit=True):
    old = st.text_input("Текущий пароль", type="password", autocomplete="current-password")
    new = st.text_input("Новый пароль", type="password", autocomplete="new-password", help="Не меньше 8 символов")
    repeat = st.text_input("Новый пароль ещё раз", type="password", autocomplete="new-password")
    if st.form_submit_button("Сменить пароль"):
        if new != repeat:
            st.error("Новые пароли не совпадают")
        else:
            try:
                store().change_password(user.id, old, new)
            except AccountError as error:
                st.error(str(error))
            else:
                st.success("Пароль изменён")

if st.button("Выйти из аккаунта"):
    sign_out()
    flash("Вы вышли из аккаунта")
    st.rerun()
