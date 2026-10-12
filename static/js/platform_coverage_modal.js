(() => {
    'use strict';
    const modal = document.getElementById('platformCoverageModal');
    if (!modal) return;
    const body = modal.querySelector('[data-coverage-content]');
    let pending;
    async function load(url) {
        if (pending) pending.abort();
        const controller = new AbortController();
        pending = controller;
        body.textContent = '正在加载平台覆盖…';
        body.setAttribute('aria-busy', 'true');
        try {
            const response = await fetch(url, { credentials: 'same-origin', signal: controller.signal });
            if (!response.ok) throw new Error('load');
            const page = new DOMParser().parseFromString(await response.text(), 'text/html');
            const section = page.querySelector('.upstream-coverage');
            if (!section) throw new Error('content');
            if (pending !== controller) return;
            section.querySelector('.coverage-header > div')?.remove();
            section.removeAttribute('aria-labelledby');
            body.replaceChildren(section);
        } catch (error) {
            if (error.name !== 'AbortError' && pending === controller) {
                body.textContent = '平台覆盖加载失败。';
                const retry = document.createElement('button');
                retry.type = 'button';
                retry.className = 'btn btn-outline-secondary ms-2';
                retry.textContent = '重试';
                retry.addEventListener('click', () => load(url));
                body.append(retry);
            }
        } finally {
            if (pending === controller) body.removeAttribute('aria-busy');
        }
    }
    modal.addEventListener('show.bs.modal', () => load(modal.dataset.coverageUrl));
    modal.addEventListener('hidden.bs.modal', () => { pending?.abort(); pending = null; });
    body.addEventListener('submit', event => {
        const form = event.target.closest('.coverage-filters');
        if (!form) return;
        event.preventDefault();
        const url = new URL(modal.dataset.coverageUrl, location.origin);
        url.search = new URLSearchParams(new FormData(form)).toString();
        load(url);
    });
    body.addEventListener('click', event => {
        const link = event.target.closest('.coverage-pagination a');
        if (!link) return;
        event.preventDefault();
        load(new URL(link.getAttribute('href'), new URL(modal.dataset.coverageUrl, location.origin)));
    });
})();
