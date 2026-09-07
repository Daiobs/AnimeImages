(() => {
    const config = document.getElementById('mail-config-form');
    if (!config) return;
    const test = document.getElementById('mail-test-form');
    const source = document.getElementById('id_source');
    const provider = document.getElementById('id_provider');
    function update() {
        const database = document.getElementById('mail-database-fields');
        const active = source.value === 'database';
        database.hidden = !active;
        database.querySelectorAll('input, select, textarea').forEach(control => {
            control.disabled = !active;
        });
        config.querySelectorAll('[data-provider]').forEach(section => {
            section.hidden = section.dataset.provider !== provider.value;
            section.querySelectorAll('input, select, textarea').forEach(control => {
                control.disabled = !active || section.hidden;
            });
        });
    }
    config.addEventListener('input', () => {
        document.getElementById('mail-dirty').hidden = false;
        if (test) test.querySelector('button').disabled = true;
    });
    config.addEventListener('change', update);
    [config, test].filter(Boolean).forEach(form => form.addEventListener('submit', () => {
        form.querySelector('button').disabled = true;
    }));
    update();
})();
