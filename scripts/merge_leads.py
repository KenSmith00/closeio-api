#!/usr/bin/env python

"""
Detect duplicate leads and merge them.

This script will loop over ALL of your leads by default and then search for
other leads like it which means this process can take quite a long time
depending on how many leads you have in your organization.

Duplicate criteria:
    - Company:  Case insensitive exact match by Company Name.
    - Email: Case insensitive exact match on any contact's email address
      within a lead.
    - Phone: Exact match on any contact's phone number wwithin a lead.

Priority (how to choose 'Destination lead'):
    - Leads with Opportunities over ones without.
    - Leads which were created first.

Beware Of:
    - There is currently a limit of 200 contacts per lead and 100 emails and
      100 phones per contact. The "in" search query will likely barf as you
      get close to 1000 arguments so the find_duplicates_for_lead() needs to
      be refactored to accomodate a merge where we have lots of contacts and
      emails/phones within those contacts. It's also unclear what happens when
      you merge two leads with 201 unique contacts between them...
    - Merging A->B and then B->A may result in a race condition where A and B
      are lost.

If you have any questions about this script please contact support@close.io.

Todo:
    - Check based on display_name, not just lead name/company.
    - Add a progress bar.
"""

import logging
import sys
import argparse
from closeio_api import Client as CloseIO_API
from progressbar import ProgressBar
from progressbar.widgets import Counter, Percentage, Bar, AdaptiveETA, FileTransferSpeed


parser = argparse.ArgumentParser(description='Detect duplicates & merge leads (see source code for details)')
parser.add_argument('--api-key', '-k', required=True, help='API Key')
parser.add_argument('--field', '-f', required=False, default='company', choices=['company', 'email', 'phone'], help='Field to compare uniqueness.')
parser.add_argument('--verbose', '-v', action='store_true', help='Increase logging verbosity.')
parser.add_argument('--development', action='store_true', help='Use a development (testing) server rather than production.')
parser.add_argument('--confirmed', action='store_true', help='Without this flag, no action will be taken (dry run). Use this to perform the merge.')
args = parser.parse_args()

api = CloseIO_API(args.api_key, development=args.development)


def setup_logger():
    logger = logging.getLogger('closeio.api.merge_leads')
    logger.setLevel(logging.INFO)
    if args.verbose:
        logger.setLevel(logging.DEBUG)

    ch = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    return logger

logger = setup_logger()


def find_duplicates_for_lead(lead, comparator_field):
    """
    Find and return duplicate leads for a given lead based on a comparator
    field. For example, if comparator field is 'phone', and contacts
    associated with leads A, B, and C have the same phone number, then calling
    this function for lead A should return leads B and C.
    """
    assert lead and comparator_field

    duplicates = []

    search_values = None
    if comparator_field == 'company':
        search_values = [lead['name']]
    else:
        comparator_field_plural = '{0}s'.format(comparator_field)
        search_values = [e[comparator_field]
                         for c in lead['contacts']
                         for e in c[comparator_field_plural]]

        # compute this lead's data points we're going to compare against (such
        # as emails or phone numbers)
        lead_elems = set()
        for contact in lead['contacts']:
            for elem in contact['{}s'.format(comparator_field)]:
                lead_elems.add(elem[comparator_field])

    if search_values:
        query = '{0} in ({1}) sort:date_created'.format(
            comparator_field,
            ', '.join('"%s"' % val.encode('utf-8') for val in search_values)
        )
        logger.debug('query = %s', query)

        has_more = True
        offset = 0
        while has_more:
            resp = api.get('lead', params={
                'query': query,
                '_skip': offset,
                '_fields': 'id,display_name,name,status_label,contacts,opportunities'
            })
            leads = resp['data']
            logger.debug('Fetched %d of %d duplicate leads.', len(leads), resp['total_results'])

            # Add leads to our list of duplicates iff they are exact matches,
            # making sure to exclude the original lead from our duplicates
            for l in leads:
                if l['id'] == lead['id']:
                    logger.debug('Removed lead %s from duplicate search results.', l['id'])
                    continue
                if comparator_field == 'company':
                    if lead['name'].strip().lower() == l['name'].strip().lower():
                        duplicates.append(l)
                else:
                    l_elems = set()
                    for contact in l['contacts']:
                        for elem in contact[comparator_field_plural]:
                            l_elems.add(elem[comparator_field])

                    logger.debug('%s (%s) %ss: ["%s"]', l['id'], l['display_name'], comparator_field, ','.join(l_elems))

                    # if at least one of the phones/emails/etc. is a match,
                    # consider this lead a duplicate
                    intersection = lead_elems & l_elems
                    if intersection:
                        duplicates.append(l)

            offset += len(leads)
            has_more = resp['has_more']

    return duplicates


def merge_lead(destination_lead, duplicates):
    """Merge all the duplicates into the destionation lead one by one."""

    # don't do anything if the --confirmed flag wasn't set
    if not args.confirmed:
        return

    for source_lead in duplicates:
        resp = api.post('lead/merge', data={
            'source': source_lead['id'],
            'destination': destination_lead['id'],
        })
        logger.info("Merged source:%s (%s) and destination:%s (%s) response_body:%s",
                    source_lead['id'], source_lead['display_name'], destination_lead['id'],
                    destination_lead['display_name'], resp)


if __name__ == "__main__":

    has_more = True
    offset = 0
    total_leads_merged = 0
    first_iteration = True

    while has_more:
        resp = api.get('lead', params={
            'query': 'sort:date_created',  # sort by date_created so that the oldest lead is always merged into
            '_skip': offset,
            '_fields': 'id,display_name,name,contacts,status_label,opportunities'
        })
        leads = resp['data']
        leads_merged_this_page = 0
        duplicates_this_page = set()

        if first_iteration:
            total_leads = resp['total_results']
            progress_widgets = ['Analyzing %d Leads: ' % total_leads, Counter(), ' ', Percentage(), ' ', Bar(), ' ', AdaptiveETA(), ' ', FileTransferSpeed()]
            pbar = ProgressBar(widgets=progress_widgets, maxval=total_leads).start()
            pbar.update(offset)
            first_iteration = False

        for idx, lead in enumerate(leads):
            logger.debug("-------------------------------------------------")
            logger.debug("idx: %d, lead: %s (%s)", idx, lead['id'], lead['display_name'])
            logger.debug("duplicates_this_page: %s", duplicates_this_page)

            # To avoid race conditions we skip over leads we've already seen
            # in our duplicates lists (see README at top of file)
            if lead['id'] in duplicates_this_page:
                logger.debug("skipping lead %s", lead['id'])
            else:
                duplicates = find_duplicates_for_lead(lead, args.field)
                duplicates_this_page |= set(x['id'] for x in duplicates)
                if duplicates:
                    logger.info('%s (%s): %d duplicates: %s', lead['id'], lead['display_name'],
                                len(duplicates), ', '.join([d['id'] for d in duplicates]))
                    merge_lead(lead, duplicates)
                    leads_merged_this_page += len(duplicates) + 1  # +1 for the destination lead
                    total_leads_merged += 1

            # Progress bar can overflow if some leads were added between the
            # first iteration of this loop and now. We just show the maxval
            # in such cases.
            if pbar.currval + 1 > pbar.maxval:
                pbar.maxval = pbar.currval + 1
            pbar.update(pbar.currval + 1)

        # We subtract the number of leads merged since those no longer exist.
        offset += max(0, len(leads) - leads_merged_this_page)
        has_more = resp['has_more']

    pbar.finish()
    logger.info("*** Merging Complete ***")
    logger.info("Total Leads Merged: %d", total_leads_merged)

