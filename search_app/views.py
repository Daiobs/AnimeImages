from pathlib import Path

from django.conf import settings
from django.core.paginator import Paginator
from django.shortcuts import render


IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def search_view(request):
    return render(request, "search.html")


def has_path_separator(value):
    return "/" in value or "\\" in value


def list_image_names(directory):
    if not directory.exists():
        return []
    return [
        path.name
        for path in sorted(directory.iterdir())
        if path.is_file() and path.name.lower().endswith(IMAGE_EXTENSIONS)
    ]


def find_character_dir(categories_dir, query):
    if not categories_dir.exists():
        return None, None

    for category_dir in sorted(categories_dir.iterdir()):
        if not category_dir.is_dir():
            continue

        character_dir = category_dir / query
        if character_dir.exists() and character_dir.is_dir():
            return category_dir.name, character_dir

    return None, None


def paginate_images(request, images, per_page=9):
    paginator = Paginator(images, per_page)
    page_number = request.GET.get("page", 1)
    return paginator.get_page(page_number)


def list_search_folders(character_dir):
    folders = {
        path.name
        for path in sorted(character_dir.iterdir())
        if path.is_dir() and path.name != "padded"
    }

    padded_dir = character_dir / "padded"
    if padded_dir.exists():
        folders.update(
            path.name
            for path in sorted(padded_dir.iterdir())
            if path.is_dir()
        )

    return sorted(folders)


def search_results_view(request):
    query = request.GET.get("q", "").strip()
    selected_folder = request.GET.get("folder")
    categories_dir = Path(settings.BASE_DIR) / "static" / "images"

    context = {
        "query": query,
        "folders": [],
        "selected_folder": selected_folder,
        "images": [],
        "page_obj": None,
    }

    if not query or has_path_separator(query):
        context["page_obj"] = paginate_images(request, [])
        return render(request, "results.html", context)

    category, character_dir = find_character_dir(categories_dir, query)
    if not character_dir:
        context["page_obj"] = paginate_images(request, [])
        return render(request, "results.html", context)

    folders = list_search_folders(character_dir)
    context["folders"] = folders

    if folders:
        if selected_folder not in folders:
            selected_folder = folders[0]
        source_dir = character_dir / "padded" / selected_folder
        url_prefix = f"/static/images/{category}/{query}/padded/{selected_folder}"
    else:
        selected_folder = None
        source_dir = character_dir / "padded"
        if not source_dir.exists():
            source_dir = character_dir
            url_prefix = f"/static/images/{category}/{query}"
        else:
            url_prefix = f"/static/images/{category}/{query}/padded"

    image_names = list_image_names(source_dir)
    page_obj = paginate_images(request, image_names)
    context["selected_folder"] = selected_folder
    context["page_obj"] = page_obj
    context["images"] = [
        {
            "url": f"{url_prefix}/{image_name}",
            "name": image_name,
        }
        for image_name in page_obj
    ]

    return render(request, "results.html", context)
