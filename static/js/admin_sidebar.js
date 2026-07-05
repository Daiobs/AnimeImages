(function () {
    const storageKey = "animeimages.admin.sidebar.collapsed";
    const groupStoragePrefix = "animeimages.admin.sidebar.group.";
    const scrollStorageKey = "animeimages.admin.sidebar.scrollTop";
    const root = document.documentElement;
    const toggle = document.querySelector("[data-admin-sidebar-toggle]");
    const nav = document.querySelector(".admin-sidebar-nav");
    const groupToggles = Array.from(document.querySelectorAll("[data-sidebar-group-toggle]"));

    function applyCollapsed(collapsed) {
        root.classList.toggle("admin-sidebar-collapsed", collapsed);
        if (toggle) {
            toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
        }
    }

    function storageGet(key) {
        try {
            return window.localStorage.getItem(key);
        } catch (error) {
            return null;
        }
    }

    function storageSet(key, value) {
        try {
            window.localStorage.setItem(key, value);
        } catch (error) {
            // Storage is only used to keep the admin shell comfortable between page loads.
        }
    }

    function sessionGet(key) {
        try {
            return window.sessionStorage.getItem(key);
        } catch (error) {
            return null;
        }
    }

    function sessionSet(key, value) {
        try {
            window.sessionStorage.setItem(key, value);
        } catch (error) {
            // The sidebar remains usable if sessionStorage is unavailable.
        }
    }

    function initialCollapsed() {
        const stored = storageGet(storageKey);
        if (stored === "1") {
            return true;
        }
        if (stored === "0") {
            return false;
        }
        return window.matchMedia("(max-width: 900px)").matches;
    }

    function setGroupCollapsed(group, collapsed) {
        const button = group.querySelector("[data-sidebar-group-toggle]");
        group.classList.toggle("is-collapsed", collapsed);
        if (button) {
            button.setAttribute("aria-expanded", collapsed ? "false" : "true");
        }
    }

    function groupStorageKey(group) {
        return groupStoragePrefix + group.getAttribute("data-sidebar-group");
    }

    function applyInitialGroupState() {
        groupToggles.forEach(function (button) {
            const group = button.closest("[data-sidebar-group]");
            if (!group) {
                return;
            }
            const stored = storageGet(groupStorageKey(group));
            const hasActiveItem = group.classList.contains("is-active-group");
            const isOverviewGroup = group.getAttribute("data-sidebar-group") === "group-0";
            if (stored === "0" && !hasActiveItem) {
                setGroupCollapsed(group, true);
                return;
            }
            if (stored === "1" || hasActiveItem || isOverviewGroup) {
                setGroupCollapsed(group, false);
                return;
            }
            setGroupCollapsed(group, true);
        });
    }

    function restoreSidebarScroll() {
        if (!nav) {
            return;
        }
        const stored = Number.parseInt(sessionGet(scrollStorageKey) || "0", 10);
        if (Number.isFinite(stored) && stored > 0) {
            nav.scrollTop = stored;
            return;
        }
        const activeLink = nav.querySelector(".admin-sidebar-link.is-active");
        if (activeLink) {
            activeLink.scrollIntoView({block: "nearest"});
        }
    }

    applyCollapsed(initialCollapsed());
    applyInitialGroupState();
    restoreSidebarScroll();

    if (nav) {
        nav.addEventListener("scroll", function () {
            sessionSet(scrollStorageKey, String(nav.scrollTop));
        }, {passive: true});
        nav.addEventListener("click", function () {
            sessionSet(scrollStorageKey, String(nav.scrollTop));
        });
    }

    if (toggle) {
        toggle.addEventListener("click", function () {
            const collapsed = !root.classList.contains("admin-sidebar-collapsed");
            applyCollapsed(collapsed);
            storageSet(storageKey, collapsed ? "1" : "0");
        });
    }

    groupToggles.forEach(function (button) {
        button.addEventListener("click", function () {
            const group = button.closest("[data-sidebar-group]");
            if (!group) {
                return;
            }
            const collapsed = !group.classList.contains("is-collapsed");
            setGroupCollapsed(group, collapsed);
            storageSet(groupStorageKey(group), collapsed ? "0" : "1");
        });
    });
})();
