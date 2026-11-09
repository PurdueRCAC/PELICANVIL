// Shown via <img onerror> wherever a category icon is rendered (here,
// admin.js's edit-category preview, and inline in index.html's static
// featured-category cards) for a category with no uploaded icon yet — a
// plain generic-image glyph, not a broken-image icon. Same constant name
// duplicated in each of those files rather than pulled from one shared
// script, since index.html doesn't otherwise load categories.js or
// admin.js and a one-line data URI isn't worth a new shared include for.
const CATEGORY_ICON_FALLBACK = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23888888' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='3' y='3' width='18' height='18' rx='3'/%3E%3Ccircle cx='8.5' cy='8.5' r='1.5'/%3E%3Cpath d='M21 15l-5-5L5 21'/%3E%3C/svg%3E";

async function fetchCategories(){
    const emptyState = document.querySelector(".category-empty-state");
    const emptyStateText = emptyState ? emptyState.querySelector(".category-empty-state-text") : null;
    try{
        const response = await fetch(`${ROOT_PATH}/retrieve-categories`);
        if(!response.ok){
            throw new Error(`HTTP ERROR! Status ${response.status}`);
        }
        const categories = await response.json();
        if(categories.length === 0){
            if(emptyStateText) emptyStateText.textContent = "No categories have been added yet.";
            if(emptyState) emptyState.style.display = "flex";
            return;
        }
        categories.forEach(category => {
            addCategoryCard(category);
        })
    }catch (error){
        console.log('Fetching categories failed: ', error);
        showToast("Couldn't load categories. Try refreshing the page.", "error");
        if(emptyStateText) emptyStateText.textContent = "Something went wrong loading categories.";
        if(emptyState) emptyState.style.display = "flex";
    }
}
async function addCategoryCard(category){
    try{
        const newCard = document.createElement("div");
        const container = document.querySelector(".category-card-container")
        newCard.className="category-card";
        const count = category["dataset_count"] || 0
        const countLabel = count === 1 ? "1 dataset" : `${count} datasets`
        newCard.innerHTML=`
        <a href="${window.ROOT_PATH}/datasets/category/${category["url"]}">
            <div class="category-card-content">
                <div class="category-card-icon">
                    <img src="${window.ROOT_PATH}/categories/${category["url"]}/icon" alt="category card icon" onerror="this.onerror=null;this.src=CATEGORY_ICON_FALLBACK;"></img>
                </div>
                <h2 class="category-card-name">${category["name"]}</h2>
                <p class="category-card-description-text">${category["description"]}</p>
                <span class="category-card-count">${countLabel}</span>
            </div>
        </a>
        `
        container.appendChild(newCard)
    }catch{

    }

}
function initCategorySearch(){
    const input = document.querySelector(".category-search-input");
    const button = document.querySelector(".category-search-submit");
    function submitSearch(){
        const query = input.value.trim();
        if(!query) return;
        window.location.href = `${window.ROOT_PATH}/datasets/search?search=${encodeURIComponent(query)}`;
    }
    button.addEventListener("click", submitSearch);
    input.addEventListener("keydown", (event) => {
        if(event.key === "Enter"){
            submitSearch();
        }
    });
}
function main(){
    document.addEventListener("DOMContentLoaded", (event) => {
        fetchCategories();
        initCategorySearch();
    })
}
main();