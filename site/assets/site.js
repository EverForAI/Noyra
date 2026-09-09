"use strict";

const menu = document.querySelector(".menu-toggle");
const mobile = document.querySelector(".mobile-nav");
if (menu && mobile) {
    document.documentElement.classList.add("js");
    const setMenu = (open) => {
        menu.setAttribute("aria-expanded", String(open));
        menu.setAttribute(
            "aria-label",
            open ? menu.dataset.closeLabel : menu.dataset.openLabel,
        );
        mobile.classList.toggle("is-open", open);
    };
    menu.addEventListener("click", () =>
        setMenu(menu.getAttribute("aria-expanded") !== "true"),
    );
    mobile.addEventListener("click", (event) => {
        if (event.target.closest("a")) setMenu(false);
    });
    document.addEventListener("keydown", (event) => {
        if (
            event.key === "Escape" &&
            menu.getAttribute("aria-expanded") === "true"
        ) {
            setMenu(false);
            menu.focus();
        }
    });
    document.addEventListener("click", (event) => {
        if (!event.target.closest(".site-header")) setMenu(false);
    });
    matchMedia("(min-width: 901px)").addEventListener("change", () =>
        setMenu(false),
    );
}
