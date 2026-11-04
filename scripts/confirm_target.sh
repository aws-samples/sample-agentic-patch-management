#!/bin/bash
# Sourced by deploy.sh and sample-env.sh.
#
# A profile or Region exported in the shell wins over .env, which can silently
# point a run at the wrong account or Region. confirm_target prints the target
# and, when the shell and .env name a different profile, makes the operator
# confirm the account (typed at a terminal, or PATCHY_CONFIRM_ACCOUNT when
# unattended). A different Region is confirmed the same way, with the Region
# typed or PATCHY_CONFIRM_REGION.
#
# Usage: confirm_target <command> <.env profile> <shell profile> [<.env region> <shell region>]
# Reads AWS_PROFILE, AWS_REGION and CDK_DEFAULT_ACCOUNT. Exits on refusal.

# _confirm_value <what> <expected> <preset value> <preset variable name>
_confirm_value() {
    local what="$1" expected="$2" preset="$3" var="$4"
    if [ -n "$preset" ]; then
        if [ "$preset" != "$expected" ]; then
            echo -e "\033[0;31m❌ $var=$preset does not match $expected. Nothing changed.\033[0m"
            exit 1
        fi
    elif [ -t 0 ]; then
        local typed
        read -p "  Type the $what to continue: " typed
        if [ "$typed" != "$expected" ]; then
            echo "Cancelled. Nothing changed."
            exit 1
        fi
    else
        echo -e "\033[0;31m❌ Not continuing unattended. Set $var=$expected to confirm.\033[0m"
        exit 1
    fi
}

confirm_target() {
    local command="$1" env_profile="$2" shell_profile="$3" env_region="${4:-}" shell_region="${5:-}"
    case "$command" in
        status|help|--help|-h) return 0 ;;
    esac
    echo "Target: profile $AWS_PROFILE, account $CDK_DEFAULT_ACCOUNT, region $AWS_REGION"
    if [ -n "$env_profile" ] && [ -n "$shell_profile" ] && [ "$shell_profile" != "$env_profile" ]; then
        echo -e "\033[1;33m⚠️  Your shell sets AWS_PROFILE=$shell_profile, overriding AWS_PROFILE=$env_profile in .env.\033[0m"
        echo "  This run would change account $CDK_DEFAULT_ACCOUNT. Unset AWS_PROFILE to use .env's."
        _confirm_value "account ID" "$CDK_DEFAULT_ACCOUNT" "${PATCHY_CONFIRM_ACCOUNT:-}" PATCHY_CONFIRM_ACCOUNT
    fi
    if [ -n "$env_region" ] && [ -n "$shell_region" ] && [ "$shell_region" != "$env_region" ]; then
        echo -e "\033[1;33m⚠️  Your shell sets AWS_REGION=$shell_region, overriding AWS_REGION=$env_region in .env.\033[0m"
        echo "  This run would change Region $AWS_REGION. Unset AWS_REGION to use .env's."
        _confirm_value "Region" "$AWS_REGION" "${PATCHY_CONFIRM_REGION:-}" PATCHY_CONFIRM_REGION
    fi
}
