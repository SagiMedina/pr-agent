from __future__ import annotations

import difflib
import logging
from typing import Any, Dict, Tuple

from pr_agent.algo.git_patch_processing import extend_patch, handle_patch_deletions
from pr_agent.algo.language_handler import sort_files_by_main_languages
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.config_loader import settings
from pr_agent.git_providers import GithubProvider

OUTPUT_BUFFER_TOKENS = 800
PATCH_EXTRA_LINES = 3


def get_pr_diff(git_provider: [GithubProvider, Any], token_handler: TokenHandler) -> str:
    """
    Returns a string with the diff of the PR.
    If needed, apply diff minimization techniques to reduce the number of tokens
    """
    files = list(git_provider.get_diff_files())

    # get pr languages
    pr_languages = sort_files_by_main_languages(git_provider.get_languages(), files)

    # generate a standard diff string, with patch extension
    patches_extended, total_tokens = pr_generate_extended_diff(pr_languages, token_handler)

    # if we are under the limit, return the full diff
    if total_tokens + OUTPUT_BUFFER_TOKENS < token_handler.limit:
        return "\n".join(patches_extended)

    # if we are over the limit, start pruning
    patches_compressed = pr_generate_compressed_diff(pr_languages, token_handler)
    return "\n".join(patches_compressed)


def pr_generate_extended_diff(pr_languages: list, token_handler: TokenHandler) -> \
        Tuple[list, int]:
    """
    Generate a standard diff string, with patch extension
    """
    total_tokens = token_handler.prompt_tokens  # initial tokens
    patches_extended = []
    for lang in pr_languages:
        for file in lang['files']:
            original_file_content_str = file.base_file
            new_file_content_str = file.head_file
            patch = file.patch

            # handle the case of large patch, that initially was not loaded
            patch = load_large_diff(file, new_file_content_str, original_file_content_str, patch)

            if not patch:
                continue

            # extend each patch with extra lines of context
            extended_patch = extend_patch(original_file_content_str, patch, num_lines=PATCH_EXTRA_LINES)
            full_extended_patch = f"## {file.filename}\n\n{extended_patch}\n"

            patch_tokens = token_handler.count_tokens(full_extended_patch)
            file.tokens = patch_tokens
            total_tokens += patch_tokens
            patches_extended.append(full_extended_patch)

    return patches_extended, total_tokens


def pr_generate_compressed_diff(top_langs: list, token_handler: TokenHandler) -> list:
    # Apply Diff Minimization techniques to reduce the number of tokens:
    # 0. Start from the largest diff patch to smaller ones
    # 1. Don't use extend context lines around diff
    # 2. Minimize deleted files
    # 3. Minimize deleted hunks
    # 4. Minimize all remaining files when you reach token limit

    patches = []

    # sort each one of the languages in top_langs by the number of tokens in the diff
    sorted_files = []
    for lang in top_langs:
        sorted_files.extend(sorted(lang['files'], key=lambda x: x.tokens, reverse=True))

    total_tokens = token_handler.prompt_tokens
    for file in sorted_files:
        original_file_content_str = file.base_file
        new_file_content_str = file.head_file
        patch = file.patch
        patch = load_large_diff(file, new_file_content_str, original_file_content_str, patch)
        if not patch:
            continue

        # removing delete-only hunks
        patch = handle_patch_deletions(patch, original_file_content_str,
                                       new_file_content_str, file.filename)
        new_patch_tokens = token_handler.count_tokens(patch)

        if total_tokens > token_handler.limit - OUTPUT_BUFFER_TOKENS // 2:
            logging.warning(f"File was fully skipped, no more tokens: {file.filename}.")
            continue  # Hard Stop, no more tokens
        if total_tokens + new_patch_tokens > token_handler.limit - OUTPUT_BUFFER_TOKENS:
            # Current logic is to skip the patch if it's too large
            # TODO: Option for alternative logic to remove hunks from the patch to reduce the number of tokens
            #  until we meet the requirements
            if settings.config.verbosity_level >= 2:
                logging.warning(f"Patch too large, minimizing it, {file.filename}")
            patch = "File was modified"
        if patch:
            patch_final = f"## {file.filename}\n\n{patch}\n"
            patches.append(patch_final)
            total_tokens += token_handler.count_tokens(patch_final)
            if settings.config.verbosity_level >= 2:
                logging.info(f"Tokens: {total_tokens}, last filename: {file.filename}")
    return patches


def load_large_diff(file, new_file_content_str: str, original_file_content_str: str, patch: str) -> str:
    if not patch:  # to Do - also add condition for file extension
        try:
            diff = difflib.unified_diff(original_file_content_str.splitlines(keepends=True),
                                        new_file_content_str.splitlines(keepends=True))
            if settings.config.verbosity_level >= 2:
                logging.warning(f"File was modified, but no patch was found. Manually creating patch: {file.filename}.")
            patch = ''.join(diff)
        except Exception:
            pass
    return patch
