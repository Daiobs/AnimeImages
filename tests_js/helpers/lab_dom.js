"use strict";

const dataKey = (name) => name.slice(5).replace(/-([a-z])/g, (_all, letter) => letter.toUpperCase());

class Node {
    constructor(tag = "div", attrs = {}) {
        this.tagName = tag.toUpperCase(); this.dataset = {}; this.children = []; this.attributes = {};
        this.listeners = {}; this.style = {}; this.hidden = false; this.disabled = false; this.value = "";
        this.className = ""; this.textContent = "";
        this.classList = {add: (name) => { this.className += ` ${name}`; }, remove() {}, toggle() {}};
        Object.entries(attrs).forEach(([key, value]) => this.setAttribute(key, value));
    }
    setAttribute(name, value) {
        this.attributes[name] = String(value);
        if (name.startsWith("data-")) this.dataset[dataKey(name)] = String(value);
        if (name === "class") this.className = String(value);
        if (name === "value") this.value = String(value);
    }
    getAttribute(name) { return (name.startsWith("data-") ? this.dataset[dataKey(name)] : this.attributes[name]) ?? null; }
    append(...nodes) { nodes.forEach((node) => { if (typeof node === "string") return; node.remove(); node.parentNode = this; this.children.push(node); }); }
    insertBefore(node, reference) {
        if (node === reference) return node;
        if (reference && reference.parentNode !== this) throw new Error("Reference is not a child");
        node.remove(); node.parentNode = this;
        this.children.splice(reference ? this.children.indexOf(reference) : this.children.length, 0, node);
        return node;
    }
    get nextSibling() { return this.parentNode?.children[this.parentNode.children.indexOf(this) + 1] || null; }
    replaceChildren(...nodes) { this.children.forEach((node) => { node.parentNode = null; }); this.children = []; this.append(...nodes); }
    remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((node) => node !== this); this.parentNode = null; }
    matches(selector) {
        const tag = selector.match(/^[a-z]+/i)?.[0];
        if (tag && this.tagName !== tag.toUpperCase()) return false;
        const cls = selector.match(/^\.([\w-]+)/)?.[1];
        if (cls && !this.className.split(/\s+/).includes(cls)) return false;
        const attrs = Array.from(selector.matchAll(/\[([^=\]]+)(?:=["']([^"']*)["'])?\]/g));
        return attrs.every(([, key, value]) => this.getAttribute(key) !== null && (value === undefined || this.getAttribute(key) === value));
    }
    querySelectorAll(selector) {
        const selectors = selector.split(/,\s*/);
        const rows = [];
        const walk = (node) => node.children.forEach((child) => {
            if (selectors.some((part) => child.matches(part))) rows.push(child);
            walk(child);
        });
        walk(this); return rows;
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    closest(selector) { return this.matches(selector) ? this : this.parentNode?.closest(selector) || null; }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    removeEventListener(name) { delete this.listeners[name]; }
    scrollIntoView() {}
}

function documentFor(root) {
    const doc = new Node("document"); doc.append(root);
    doc.createElement = (tag) => new Node(tag);
    doc.createTextNode = (value) => Object.assign(new Node("text"), {textContent: value});
    doc.createDocumentFragment = () => new Node("fragment");
    return doc;
}

module.exports = {Node, documentFor};
