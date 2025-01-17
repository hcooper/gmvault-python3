'''
    Gmvault: a tool to backup and restore your gmail account.
    Copyright (C) <since 2011>  <guillaume Aubert (guillaume dot aubert at gmail do com)>

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as
    published by the Free Software Foundation, either version 3 of the
    License, or (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.

'''
import json
import time
import datetime
import os
import itertools
import imaplib
from multiprocessing import Process, Queue

import log_utils

import collections_utils
import gmvault_utils
import imap_utils
import gmvault_db

LOG = log_utils.LoggerFactory.get_logger('gmvault')

def handle_restore_imap_error(the_exception, gm_id, db_gmail_ids_info, gmvaulter):
    """
       function to handle restore IMAPError in restore functions 
    """
    if isinstance(the_exception, imaplib.IMAP4.abort):
        # if this is a Gmvault SSL Socket error quarantine the email and continue the restore
        if str(the_exception).find("=> Gmvault ssl socket error: EOF") >= 0:
            LOG.critical("Quarantine email with gm id %s from %s. GMAIL IMAP cannot restore it:"\
                         " err={%s}" % (gm_id, db_gmail_ids_info[gm_id], str(the_exception)))
            gmvaulter.gstorer.quarantine_email(gm_id)
            gmvaulter.error_report['emails_in_quarantine'].append(gm_id)
            LOG.critical("Disconnecting and reconnecting to restart cleanly.")
            gmvaulter.src.reconnect() #reconnect
        else:
            raise the_exception
        
    elif isinstance(the_exception, imaplib.IMAP4.error): 
        LOG.error("Catched IMAP Error %s" % (str(the_exception)))
        LOG.exception(the_exception)
        
        #When the email cannot be read from Database because it was empty when returned by gmail imap
        #quarantine it.
        if str(the_exception) == "APPEND command error: BAD ['Invalid Arguments: Unable to parse message']":
            LOG.critical("Quarantine email with gm id %s from %s. GMAIL IMAP cannot restore it:"\
                         " err={%s}" % (gm_id, db_gmail_ids_info[gm_id], str(the_exception)))
            gmvaulter.gstorer.quarantine_email(gm_id)
            gmvaulter.error_report['emails_in_quarantine'].append(gm_id) 
        else:
            raise the_exception
    elif isinstance(the_exception, imap_utils.PushEmailError):
        LOG.error("Catch the following exception %s" % (str(the_exception)))
        LOG.exception(the_exception)
        
        if the_exception.quarantined():
            LOG.critical("Quarantine email with gm id %s from %s. GMAIL IMAP cannot restore it:"\
                         " err={%s}" % (gm_id, db_gmail_ids_info[gm_id], str(the_exception)))
            gmvaulter.gstorer.quarantine_email(gm_id)
            gmvaulter.error_report['emails_in_quarantine'].append(gm_id) 
        else:
            raise the_exception          
    else:
        LOG.error("Catch the following exception %s" % (str(the_exception)))
        LOG.exception(the_exception)
        raise the_exception

def handle_sync_imap_error(the_exception, the_id, error_report, src):
    """
      function to handle IMAPError in gmvault
      type = chat or email
    """    
    if isinstance(the_exception, imaplib.IMAP4.abort):
        # imap abort error 
        # ignore it 
        # will have to do something with these ignored messages
        LOG.critical("Error while fetching message with imap id %s." % (the_id))
        LOG.critical("\n=== Exception traceback ===\n")
        LOG.critical(gmvault_utils.get_exception_traceback())
        LOG.critical("=== End of Exception traceback ===\n")
        try:
            #try to get the gmail_id
            curr = src.fetch(the_id, imap_utils.GIMAPFetcher.GET_GMAIL_ID) 
        except Exception: #pylint:disable-msg=W0703
            curr = None
            LOG.critical("Error when trying to get gmail id for message with imap id %s." % (the_id))
            LOG.critical("Disconnect, wait for 20 sec then reconnect.")
            src.disconnect()
            #could not fetch the gm_id so disconnect and sleep
            #sleep 10 sec
            time.sleep(10)
            LOG.critical("Reconnecting ...")
            src.connect()
            
        if curr:
            gmail_id = curr[the_id][imap_utils.GIMAPFetcher.GMAIL_ID]
        else:
            gmail_id = None
            
        #add ignored id
        error_report['cannot_be_fetched'].append((the_id, gmail_id))
        
        LOG.critical("Forced to ignore message with imap id %s, (gmail id %s)." % (the_id, (gmail_id if gmail_id else "cannot be read")))
    elif isinstance(the_exception, imaplib.IMAP4.error):
        # check if this is a cannot be fetched error 
        # I do not like to do string guessing within an exception but I do not have any choice here
        LOG.critical("Error while fetching message with imap id %s." % (the_id))
        LOG.critical("\n=== Exception traceback ===\n")
        LOG.critical(gmvault_utils.get_exception_traceback())
        LOG.critical("=== End of Exception traceback ===\n")
         
        #quarantine emails that have raised an abort error
        if str(the_exception).find("'Some messages could not be FETCHed (Failure)'") >= 0:
            try:
                #try to get the gmail_id
                LOG.critical("One more attempt. Trying to fetch the Gmail ID for %s" % (the_id) )
                curr = src.fetch(the_id, imap_utils.GIMAPFetcher.GET_GMAIL_ID) 
            except Exception: #pylint:disable-msg=W0703
                curr = None
            
            if curr:
                gmail_id = curr[the_id][imap_utils.GIMAPFetcher.GMAIL_ID]
            else:
                gmail_id = None
            
            #add ignored id
            error_report['cannot_be_fetched'].append((the_id, gmail_id))
            
            LOG.critical("Ignore message with imap id %s, (gmail id %s)" % (the_id, (gmail_id if gmail_id else "cannot be read")))
        
        else:
            raise the_exception #rethrow error
    else:
        raise the_exception    

class IMAPBatchFetcher(object):
    """
       Fetch IMAP data in batch 
    """
    def __init__(self, src, imap_ids, error_report, request, default_batch_size = 100):
        """
           constructor
        """
        self.src                = src
        self.imap_ids           = imap_ids
        self.def_batch_size     = default_batch_size
        self.request            = request
        self.error_report       = error_report  
        
        self.to_fetch           = list(imap_ids)
    
    def individual_fetch(self, imap_ids):
        """
           Find the imap_id creating the issue
           return the data related to the imap_ids
        """
        new_data = {}
        for the_id in imap_ids:    
            try:
                
                single_data = self.src.fetch(the_id, self.request)
                new_data.update(single_data)
                
            except Exception as error:
                    handle_sync_imap_error(error, the_id, self.error_report, self.src) #do everything in this handler

        return new_data
   
    def __iter__(self):
        return self     
    
    def __next__(self):
        """
            Return the next batch of elements
        """
        new_data = {}
        batch = self.to_fetch[:self.def_batch_size]
        
        if len(batch) <= 0:
            raise StopIteration
        
        try:
        
            new_data = self.src.fetch(batch, self.request)
            
            self.to_fetch = self.to_fetch[self.def_batch_size:]
            
            return new_data

        except imaplib.IMAP4.error:
            new_data = self.individual_fetch(batch) 
    
        return new_data
    
    def reset(self):
        """
           Restart from the beginning
        """
        self.to_fetch = self.imap_ids              
               
class GMVaulter(object):
    """
       Main object operating over gmail
    """ 
    NB_GRP_OF_ITEMS         = 1400
    EMAIL_RESTORE_PROGRESS  = 'email_last_id.restore'
    CHAT_RESTORE_PROGRESS   = 'chat_last_id.restore'
    EMAIL_SYNC_PROGRESS     = 'email_last_id.sync'
    CHAT_SYNC_PROGRESS      = 'chat_last_id.sync'
    
    OP_EMAIL_RESTORE = "EM_RESTORE"
    OP_EMAIL_SYNC    = "EM_SYNC"
    OP_CHAT_RESTORE  = "CH_RESTORE"
    OP_CHAT_SYNC    = "CH_SYNC"
    
    OP_TO_FILENAME = { OP_EMAIL_RESTORE : EMAIL_RESTORE_PROGRESS,
                       OP_EMAIL_SYNC    : EMAIL_SYNC_PROGRESS,
                       OP_CHAT_RESTORE  : CHAT_RESTORE_PROGRESS,
                       OP_CHAT_SYNC     : CHAT_SYNC_PROGRESS
                     }
    
    
    def __init__(self, db_root_dir, host, port, login, credential, read_only_access = True, use_encryption = False): #pylint:disable-msg=R0913
        """
           constructor
        """   
        self.db_root_dir = db_root_dir
        
        #create dir if it doesn't exist
        gmvault_utils.makedirs(self.db_root_dir)
        
        #keep track of login email
        self.login = login
            
        # create source and try to connect
        self.src = imap_utils.GIMAPFetcher(host, port, login, credential, readonly_folder = read_only_access)
        
        self.src.connect()
        
        LOG.debug("Connected")
        
        self.use_encryption = use_encryption
        
        #to report gmail imap problems
        self.error_report = { 'empty' : [] ,
                              'cannot_be_fetched'  : [],
                              'emails_in_quarantine' : [],
                              'reconnections' : 0}
        
        #instantiate gstorer
        self.gstorer =  gmvault_db.GmailStorer(self.db_root_dir, self.use_encryption)
        
        #timer used to mesure time spent in the different values
        self.timer = gmvault_utils.Timer()
        
    @classmethod
    def get_imap_request_btw_2_dates(cls, begin_date, end_date):
        """
           Return the imap request for those 2 dates
        """
        imap_req = 'Since %s Before %s' % (gmvault_utils.datetime2imapdate(begin_date), gmvault_utils.datetime2imapdate(end_date))
        
        return imap_req
    
    def get_operation_report(self):
        """
           Return the error report
        """
        the_str = "\n================================================================\n"\
              "Number of reconnections: %d.\nNumber of emails quarantined: %d.\n" \
              "Number of emails that could not be fetched: %d.\n" \
              "Number of emails that were returned empty by gmail: %d\n================================================================" \
              % (self.error_report['reconnections'], \
                 len(self.error_report['emails_in_quarantine']), \
                 len(self.error_report['cannot_be_fetched']), \
                 len(self.error_report['empty'])
                )
              
        LOG.debug("error_report complete structure = %s" % (self.error_report))
        
        return the_str
        
    def _sync_between(self, begin_date, end_date, storage_dir, compress = True):
        """
           sync between 2 dates
        """
        #create storer
        gstorer = gmvault_db.GmailStorer(storage_dir, self.use_encryption)
        
        #search before the next month
        imap_req = self.get_imap_request_btw_2_dates(begin_date, end_date)
        
        ids = self.src.search(imap_req)
                              
        #loop over all ids, get email store email
        for the_id in ids:
            
            #retrieve email from destination email account
            data      = self.src.fetch(the_id, imap_utils.GIMAPFetcher.GET_ALL_INFO)
            
            file_path = gstorer.bury_email(data[the_id], compress = compress)
            
            LOG.critical("Stored email %d in %s" %(the_id, file_path))
        
    @classmethod
    def _get_next_date(cls, a_current_date, start_month_beginning = False):
        """
           return the next date necessary to build the imap req
        """
        if start_month_beginning:
            dummy_date   = a_current_date.replace(day=1)
        else:
            dummy_date   = a_current_date
            
        # the next date = current date + 1 month
        return dummy_date + datetime.timedelta(days=31)
        
    @classmethod
    def check_email_on_disk(cls, a_gstorer, a_id, a_dir = None):
        """
           Factory method to create the object if it exists
        """
        try:
            a_dir = a_gstorer.get_directory_from_id(a_id, a_dir)
           
            if a_dir:
                return a_gstorer.unbury_metadata(a_id, a_dir) 
            
        except ValueError as json_error:
            LOG.exception("Cannot read file %s. Try to fetch the data again" % ('%s.meta' % (a_id)), json_error )
        
        return None
    
    @classmethod
    def _metadata_needs_update(cls, curr_metadata, new_metadata, chat_metadata = False):
        """
           Needs update
        """
        if curr_metadata[gmvault_db.GmailStorer.ID_K] != new_metadata['X-GM-MSGID']:
            raise Exception("Gmail id has changed for %s" % (curr_metadata['id']))
                
        #check flags   
        prev_set = set(new_metadata['FLAGS'])    
        
        for flag in curr_metadata['flags']:
            if flag not in prev_set:
                return True
            else:
                prev_set.remove(flag)
        
        if len(prev_set) > 0:
            return True
        
        #check labels
        prev_labels = set(new_metadata['X-GM-LABELS'])
        
        if chat_metadata: #add gmvault-chats labels
            prev_labels.add(gmvault_db.GmailStorer.CHAT_GM_LABEL)
            
        
        for label in curr_metadata['labels']:
            if label not in prev_labels:
                return True
            else:
                prev_labels.remove(label)
        
        if len(prev_labels) > 0:
            return True
        
        return False
    
    
    def _check_email_db_ownership(self, ownership_control):
        """
           Check email database ownership.
           If ownership control activated then fail if a new additional owner is added.
           Else if no ownership control allow one more user and save it in the list of owners
           
           Return the number of owner this will be used to activate or not the db clean.
           Activating a db cleaning on a multiownership db would be a catastrophy as it would delete all
           the emails from the others users.
        """
        #check that the gmvault-db is not associated with another user
        db_owners = self.gstorer.get_db_owners()
        if ownership_control:
            if len(db_owners) > 0 and self.login not in db_owners: #db owner should not be different unless bypass activated
                raise Exception("The email database %s is already associated with one or many logins: %s."\
                                " Use option (-m, --multiple-db-owner) if you want to link it with %s" \
                                % (self.db_root_dir, ", ".join(db_owners), self.login))
        else:
            if len(db_owners) == 0:
                LOG.critical("Establish %s as the owner of the Gmvault db %s." % (self.login, self.db_root_dir))  
            elif len(db_owners) > 0 and self.login not in db_owners:
                LOG.critical("The email database %s is hosting emails from %s. It will now also store emails from %s" \
                             % (self.db_root_dir, ", ".join(db_owners), self.login))
                
        #try to save db_owner in the list of owners
        self.gstorer.store_db_owner(self.login)
        
    def _sync_chats(self, imap_req, compress, restart):
        """
           backup the chat messages
        """
        chat_dir = None
        
        timer = gmvault_utils.Timer() #start local timer for chat
        timer.start()
        
        LOG.debug("Before selection")
        if self.src.is_visible('CHATS'):
            chat_dir = self.src.select_folder('CHATS')
        
        LOG.debug("Selection is finished")

        if chat_dir:
            #imap_ids = self.src.search({ 'type': 'imap', 'req': 'ALL' })
            imap_ids = self.src.search(imap_req)
            
            # check if there is a restart
            if restart:
                LOG.critical("Restart mode activated. Need to find information in Gmail, be patient ...")
                imap_ids = self.get_gmails_ids_left_to_sync(self.OP_CHAT_SYNC, imap_ids)
            
            total_nb_chats_to_process = len(imap_ids) # total number of emails to get
            
            LOG.critical("%d chat messages to be fetched." % (total_nb_chats_to_process))
            
            nb_chats_processed = 0
            
            to_fetch = set(imap_ids)
            batch_fetcher = IMAPBatchFetcher(self.src, imap_ids, self.error_report, imap_utils.GIMAPFetcher.GET_ALL_BUT_DATA, \
                                       default_batch_size = gmvault_utils.get_conf_defaults().getint("General","nb_messages_per_batch",500))
        
        
            for new_data in batch_fetcher:
                for the_id in new_data: 
                    if new_data.get(the_id, None):       
                        gid = None
                        
                        LOG.debug("\nProcess imap chat id %s" % ( the_id ))
                        
                        d = new_data[the_id]
                        
                        gid = d[imap_utils.GIMAPFetcher.GMAIL_ID]
                            
                        gid = new_data[the_id][imap_utils.GIMAPFetcher.GMAIL_ID]
                        
                        the_dir      = self.gstorer.get_sub_chats_dir()
                        
                        LOG.critical("Process chat num %d (imap_id:%s) into %s." % (nb_chats_processed, the_id, the_dir))
                    
                        #pass the dir and the ID
                        curr_metadata = GMVaulter.check_email_on_disk( self.gstorer , \
                                                                       new_data[the_id][imap_utils.GIMAPFetcher.GMAIL_ID], \
                                                                       the_dir)
                        
                        #if on disk check that the data is not different
                        if curr_metadata:
                            
                            if self._metadata_needs_update(curr_metadata, new_data[the_id], chat_metadata = True):
                                
                                LOG.debug("Chat with imap id %s and gmail id %s has changed. Updated it." % (the_id, gid))
                                
                                #restore everything at the moment
                                gid  = self.gstorer.bury_chat_metadata(new_data[the_id], local_dir = the_dir)
                                
                                #update local index id gid => index per directory to be thought out
                            else:
                                LOG.debug("The metadata for chat %s already exists and is identical to the one on GMail." % (gid))
                        else:  
                            try:
                                #get the data
                                email_data = self.src.fetch(the_id, imap_utils.GIMAPFetcher.GET_DATA_ONLY )
                                
                                new_data[the_id][imap_utils.GIMAPFetcher.EMAIL_BODY] = email_data[the_id][imap_utils.GIMAPFetcher.EMAIL_BODY]
                                
                                # store data on disk within year month dir 
                                gid  = self.gstorer.bury_chat(new_data[the_id], local_dir = the_dir, compress = compress)
                                
                                #update local index id gid => index per directory to be thought out
                                LOG.debug("Create and store chat with imap id %s, gmail id %s." % (the_id, gid))   
                            except Exception as error:
                                handle_sync_imap_error(error, the_id, self.error_report, self.src) #do everything in this handler    
                    
                        nb_chats_processed += 1    
                        
                        #indicate every 50 messages the number of messages left to process
                        left_emails = (total_nb_chats_to_process - nb_chats_processed)
                        
                        if (nb_chats_processed % 50) == 0 and (left_emails > 0):
                            elapsed = timer.elapsed() #elapsed time in seconds
                            LOG.critical("\n== Processed %d emails in %s. %d left to be stored (time estimate %s).==\n" % \
                                         (nb_chats_processed,  timer.seconds_to_human_time(elapsed), \
                                          left_emails, \
                                          timer.estimate_time_left(nb_chats_processed, elapsed, left_emails)))
                        
                        # save id every 10 restored emails
                        if (nb_chats_processed % 10) == 0:
                            if gid:
                                self.save_lastid(self.OP_CHAT_SYNC, gid)
                    else:
                        LOG.info("Could not process imap with id %s. Ignore it\n")
                        self.error_report['empty'].append((the_id, None)) 
                    
                to_fetch -= set(new_data.keys()) #remove all found keys from to_fetch set
                
            for the_id in to_fetch:
                # case when gmail IMAP server returns OK without any data whatsoever
                # eg. imap uid 142221L ignore it
                LOG.info("Could not process chat with id %s. Ignore it\n")
                self.error_report['empty_chats'].append((the_id, None))

        else:
            imap_ids = []    
        
        LOG.critical("\nChats synchronisation operation performed in %s.\n" % (timer.seconds_to_human_time(timer.elapsed())))
        return imap_ids

    
    def _sync_emails(self, imap_req, compress, restart):
        """
           First part of the double pass strategy: 
           - create and update emails in db
           
        """    
        timer = gmvault_utils.Timer()
        timer.start()
           
        #select all mail folder using the constant name defined in GIMAPFetcher
        self.src.select_folder('ALLMAIL')
        
        # get all imap ids in All Mail
        imap_ids = self.src.search(imap_req)
        
        # check if there is a restart
        if restart:
            LOG.critical("Restart mode activated for emails. Need to find information in Gmail, be patient ...")
            imap_ids = self.get_gmails_ids_left_to_sync(self.OP_EMAIL_SYNC, imap_ids)
        
        total_nb_emails_to_process = len(imap_ids) # total number of emails to get
        
        LOG.critical("%d emails to be fetched." % (total_nb_emails_to_process))
        
        nb_emails_processed = 0
        
        to_fetch = set(imap_ids)
        batch_fetcher = IMAPBatchFetcher(self.src, imap_ids, self.error_report, imap_utils.GIMAPFetcher.GET_ALL_BUT_DATA, \
                                   default_batch_size = gmvault_utils.get_conf_defaults().getint("General","nb_messages_per_batch",500))
        
        #LAST Thing to do remove all found ids from imap_ids and if ids left add missing in report
        for new_data in batch_fetcher:            
            for the_id in new_data:
                if new_data.get(the_id, None):
                    LOG.debug("\nProcess imap id %s" % ( the_id ))
                        
                    gid = new_data[the_id][imap_utils.GIMAPFetcher.GMAIL_ID]
                    
                    the_dir      = gmvault_utils.get_ym_from_datetime(new_data[the_id][imap_utils.GIMAPFetcher.IMAP_INTERNALDATE])
                    
                    LOG.critical("Process email num %d (imap_id:%s) from %s." % (nb_emails_processed, the_id, the_dir))
                
                    #pass the dir and the ID
                    curr_metadata = GMVaulter.check_email_on_disk( self.gstorer , \
                                                                   new_data[the_id][imap_utils.GIMAPFetcher.GMAIL_ID], \
                                                                   the_dir)
                    
                    #if on disk check that the data is not different
                    if curr_metadata:
                        
                        LOG.debug("metadata for %s already exists. Check if different." % (gid))
                        
                        if self._metadata_needs_update(curr_metadata, new_data[the_id]):
                            
                            LOG.debug("Chat with imap id %s and gmail id %s has changed. Updated it." % (the_id, gid))
                            
                            #restore everything at the moment
                            gid  = self.gstorer.bury_metadata(new_data[the_id], local_dir = the_dir)
                            
                            #update local index id gid => index per directory to be thought out
                        else:
                            LOG.debug("On disk metadata for %s is up to date." % (gid))
                    else:  
                        try:
                            #get the data
                            LOG.debug("Get Data for %s." % (gid))
                            email_data = self.src.fetch(the_id, imap_utils.GIMAPFetcher.GET_DATA_ONLY )
                            
                            new_data[the_id][imap_utils.GIMAPFetcher.EMAIL_BODY] = email_data[the_id][imap_utils.GIMAPFetcher.EMAIL_BODY]
                            
                            # store data on disk within year month dir 
                            gid  = self.gstorer.bury_email(new_data[the_id], local_dir = the_dir, compress = compress)
                            
                            #update local index id gid => index per directory to be thought out
                            LOG.debug("Create and store email with imap id %s, gmail id %s." % (the_id, gid))   
                        except Exception as error:
                            handle_sync_imap_error(error, the_id, self.error_report, self.src) #do everything in this handler    
                    
                    nb_emails_processed += 1
                    
                    #indicate every 50 messages the number of messages left to process
                    left_emails = (total_nb_emails_to_process - nb_emails_processed)
                    
                    if (nb_emails_processed % 50) == 0 and (left_emails > 0):
                        elapsed = timer.elapsed() #elapsed time in seconds
                        LOG.critical("\n== Processed %d emails in %s. %d left to be stored (time estimate %s).==\n" % \
                                     (nb_emails_processed,  \
                                      timer.seconds_to_human_time(elapsed), left_emails, \
                                      timer.estimate_time_left(nb_emails_processed, elapsed, left_emails)))
                    
                    # save id every 10 restored emails
                    if (nb_emails_processed % 10) == 0:
                        if gid:
                            self.save_lastid(self.OP_EMAIL_SYNC, gid)
                else:
                    LOG.info("Could not process imap with id %s. Ignore it\n")
                    self.error_report['empty'].append((the_id, gid if gid else None))
                    
            to_fetch -= set(new_data.keys()) #remove all found keys from to_fetch set
                
        for the_id in to_fetch:
            # case when gmail IMAP server returns OK without any data whatsoever
            # eg. imap uid 142221L ignore it
            LOG.info("Could not process imap with id %s. Ignore it\n")
            self.error_report['empty'].append((the_id, None))
        
        LOG.critical("\nEmails synchronisation operation performed in %s.\n" % (timer.seconds_to_human_time(timer.elapsed())))
        
        return imap_ids
    
    def sync(self, imap_req = imap_utils.GIMAPFetcher.IMAP_ALL, compress_on_disk = True, db_cleaning = False, ownership_checking = True, \
            restart = False, emails_only = False, chats_only = False):
        """
           sync mode 
        """
        #check ownership to have one email per db unless user wants different
        #save the owner if new
        self._check_email_db_ownership(ownership_checking)
                
        if not compress_on_disk:
            LOG.critical("Disable compression when storing emails.")
            
        if self.use_encryption:
            LOG.critical("Encryption activated. All emails will be encrypted before to be stored.")
            LOG.critical("Please take care of the encryption key stored in (%s) or all"\
                         " your stored emails will become unreadable." % (gmvault_db.GmailStorer.get_encryption_key_path(self.db_root_dir)))
        
        self.timer.start() #start syncing emails
        
        if not chats_only:
            # backup emails
            LOG.critical("Start emails synchronization.\n")
            self._sync_emails(imap_req, compress = compress_on_disk, restart = restart)
        else:
            LOG.critical("Skip emails synchronization.\n")
        
        if not emails_only:
            # backup chats
            LOG.critical("Start chats synchronization.\n")
            self._sync_chats(imap_req, compress = compress_on_disk, restart = restart)
        else:
            LOG.critical("\nSkip chats synchronization.\n")
        
        #delete supress emails from DB since last sync
        if len(self.gstorer.get_db_owners()) <= 1:
            self.check_clean_db(db_cleaning)
        else:
            LOG.critical("Deactivate database cleaning on a multi-owners Gmvault db.")
        
        LOG.critical("Synchronisation operation performed in %s.\n" \
                     % (self.timer.seconds_to_human_time(self.timer.elapsed())))
        
        #update number of reconnections
        self.error_report["reconnections"] = self.src.total_nb_reconns
        
        return self.error_report

    
    def _delete_sync(self, imap_ids, db_gmail_ids, db_gmail_ids_info, msg_type):
        """
           Delete emails from the database if necessary
           imap_ids      : all remote imap_ids to check
           db_gmail_ids_info : info read from metadata
           msg_type : email or chat
        """
        
        # optimize nb of items
        nb_items = self.NB_GRP_OF_ITEMS if len(imap_ids) >= self.NB_GRP_OF_ITEMS else len(imap_ids)
        
        LOG.critical("Call Gmail to check the stored %ss against the Gmail %ss ids and see which ones have been deleted.\n\n"\
                     "This might take a few minutes ...\n" % (msg_type, msg_type)) 
         
        #calculate the list elements to delete
        #query nb_items items in one query to minimise number of imap queries
        for group_imap_id in itertools.zip_longest(fillvalue=None, *[iter(imap_ids)]*nb_items):
            
            # if None in list remove it
            if None in group_imap_id: 
                group_imap_id = [ im_id for im_id in group_imap_id if im_id != None ]
            
            #LOG.debug("Interrogate Gmail Server for %s" % (str(group_imap_id)))
            data = self.src.fetch(group_imap_id, imap_utils.GIMAPFetcher.GET_GMAIL_ID)
            
            # syntax for 2.7 set comprehension { data[key][imap_utils.GIMAPFetcher.GMAIL_ID] for key in data }
            # need to create a list for 2.6
            db_gmail_ids.difference_update([data[key][imap_utils.GIMAPFetcher.GMAIL_ID] for key in data ])
            
            if len(db_gmail_ids) == 0:
                break
        
        LOG.critical("Will delete %s %s(s) from gmvault db.\n" % (len(db_gmail_ids), msg_type) )
        for gm_id in db_gmail_ids:
            LOG.critical("gm_id %s not in the Gmail server. Delete it." % (gm_id))
            self.gstorer.delete_emails([(gm_id, db_gmail_ids_info[gm_id])], msg_type)
        
    def get_gmails_ids_left_to_sync(self, op_type, imap_ids):
        """
           Get the ids that still needs to be sync
           Return a list of ids
        """
        
        filename = self.OP_TO_FILENAME.get(op_type, None)
        
        if not filename:
            raise Exception("Bad Operation (%s) in save_last_id. This should not happen, send the error to the software developers." % (op_type))
        
        filepath = '%s/%s_%s' % (self.gstorer.get_info_dir(), self.login, filename)
        
        if not os.path.exists(filepath):
            LOG.critical("last_id.sync file %s doesn't exist.\nSync the full list of backed up emails." %(filepath))
            return imap_ids
        
        json_obj = json.load(open(filepath, 'r'))
        
        last_id = json_obj['last_id']
        
        last_id_index = -1
        
        new_gmail_ids = imap_ids
        
        try:
            #get imap_id from stored gmail_id
            dummy = self.src.search({'type':'imap', 'req':'X-GM-MSGID %s' % (last_id)})
            
            imap_id = dummy[0]
            last_id_index = imap_ids.index(imap_id)
            LOG.critical("Restart from gmail id %s (imap id %s)." % (last_id, imap_id))
            new_gmail_ids = imap_ids[last_id_index:]   
        except Exception: #ignore any exception and try to get all ids in case of problems. pylint:disable=W0703
            #element not in keys return current set of keys
            LOG.critical("Error: Cannot restore from last restore gmail id. It is not in Gmail."\
                         " Sync the complete list of gmail ids requested from Gmail.")
        
        return new_gmail_ids
        
    def check_clean_db(self, db_cleaning):
        """
           Check and clean the database (remove file that are not anymore in Gmail
        """
        owners = self.gstorer.get_db_owners()
        if not db_cleaning: #decouple the 2 conditions for activating cleaning
            LOG.debug("db_cleaning is off so ignore removing deleted emails from disk.")
            return
        elif len(owners) > 1:
            LOG.critical("Gmvault db hosting emails from different accounts: %s.\nCannot activate database cleaning." % (", ".join(owners)))
            return
        else:
            LOG.critical("Look for emails/chats that are in the Gmvault db but not in Gmail servers anymore.\n")
            
            #get gmail_ids from db
            LOG.critical("Read all gmail ids from the Gmvault db. It might take a bit of time ...\n")
            
            timer = gmvault_utils.Timer() # needed for enhancing the user information
            timer.start()
            
            db_gmail_ids_info = self.gstorer.get_all_existing_gmail_ids()
        
            LOG.critical("Found %s email(s) in the Gmvault db.\n" % (len(db_gmail_ids_info)) )
        
            #create a set of keys
            db_gmail_ids = set(db_gmail_ids_info.keys())
            
            # get all imap ids in All Mail
            self.src.select_folder('ALLMAIL') #go to all mail
            imap_ids = self.src.search(imap_utils.GIMAPFetcher.IMAP_ALL) #search all
            
            LOG.debug("Got %s emails imap_id(s) from the Gmail Server." % (len(imap_ids)))
            
            #delete supress emails from DB since last sync
            self._delete_sync(imap_ids, db_gmail_ids, db_gmail_ids_info, 'email')
            
            # get all chats ids
            if self.src.is_visible('CHATS'):
            
                db_gmail_ids_info = self.gstorer.get_all_chats_gmail_ids()
                
                LOG.critical("Found %s chat(s) in the Gmvault db.\n" % (len(db_gmail_ids_info)) )
                
                self.src.select_folder('CHATS') #go to chats
                chat_ids = self.src.search(imap_utils.GIMAPFetcher.IMAP_ALL)
                
                db_chat_ids = set(db_gmail_ids_info.keys())
                
                LOG.debug("Got %s chat imap_ids from the Gmail Server." % (len(chat_ids)))
            
                #delete supress emails from DB since last sync
                self._delete_sync(chat_ids, db_chat_ids, db_gmail_ids_info , 'chat')
            else:
                LOG.critical("Chats IMAP Directory not visible on Gmail. Ignore deletion of chats.")
                
            
            LOG.critical("\nDeletion checkup done in %s." % (timer.elapsed_human_time()))
            
    
    def remote_sync(self):
        """
           Sync with a remote source (IMAP mirror or cloud storage area)
        """
        #sync remotely 
        pass
        
    
    def save_lastid(self, op_type, gm_id):
        """
           Save the passed gmid in last_id.restore
           For the moment reopen the file every time
        """
        filename = self.OP_TO_FILENAME.get(op_type, None)

        if not filename:
            raise Exception("Bad Operation (%s) in save_last_id. This should not happen, send the error to the software developers." % op_type)

        filepath = '%s/%s_%s' % (self.gstorer.get_info_dir(), self.login, filename)

        with open(filepath, 'w') as f:
            json.dump({'last_id' : gm_id}, f)

    def get_gmails_ids_left_to_restore(self, op_type, db_gmail_ids_info):
        """
           Get the ids that still needs to be restored
           Return a dict key = gm_id, val = directory
        """
        filename = self.OP_TO_FILENAME.get(op_type, None)
        
        if not filename:
            raise Exception("Bad Operation (%s) in save_last_id. This should not happen, send the error to the software developers." % (op_type))
        
        
        #filepath = '%s/%s_%s' % (gmvault_utils.get_home_dir_path(), self.login, filename)
        filepath = '%s/%s_%s' % (self.gstorer.get_info_dir(), self.login, filename)
        
        if not os.path.exists(filepath):
            LOG.critical("last_id restore file %s doesn't exist.\nRestore the full list of backed up emails." %(filepath))
            return db_gmail_ids_info
        
        json_obj = json.load(open(filepath, 'r'))
        
        last_id = json_obj['last_id']
        
        last_id_index = -1
        try:
            keys = list(db_gmail_ids_info.keys())
            last_id_index = keys.index(last_id)
            LOG.critical("Restart from gmail id %s." % (last_id))
        except ValueError:
            #element not in keys return current set of keys
            LOG.error("Cannot restore from last restore gmail id. It is not in the disk database.")
        
        new_gmail_ids_info = collections_utils.OrderedDict()
        if last_id_index != -1:
            for key in list(db_gmail_ids_info.keys())[last_id_index+1:]:
                new_gmail_ids_info[key] =  db_gmail_ids_info[key]
        else:
            new_gmail_ids_info = db_gmail_ids_info    
            
        return new_gmail_ids_info 
           
    def restore(self, pivot_dir = None, extra_labels = [], restart = False, emails_only = False, chats_only = False): #pylint:disable=W0102
        """
           Restore emails in a gmail account
        """
        self.timer.start() #start restoring
        
        #self.src.select_folder('ALLMAIL') #insure that Gmvault is in ALLMAIL
        
        if not chats_only:
            # backup emails
            LOG.critical("Start emails restoration.\n")
            
            if pivot_dir:
                LOG.critical("Quick mode activated. Will only restore all emails since %s.\n" % (pivot_dir))
            
            self.restore_emails(pivot_dir, extra_labels, restart)
        else:
            LOG.critical("Skip emails restoration.\n")
        
        if not emails_only:
            # backup chats
            LOG.critical("Start chats restoration.\n")
            self.restore_chats(extra_labels, restart)
        else:
            LOG.critical("Skip chats restoration.\n")
        
        LOG.critical("Restore operation performed in %s.\n" \
                     % (self.timer.seconds_to_human_time(self.timer.elapsed())))
        
        #update number of reconnections
        self.error_report["reconnections"] = self.src.total_nb_reconns
        
        return self.error_report
       
    def common_restore(self, the_type, db_gmail_ids_info, extra_labels = [], restart = False): #pylint:disable=W0102
        """
           common_restore 
        """
        if the_type == "chats":
            msg = "chats"
            op  = self.OP_CHAT_RESTORE
        elif the_type == "emails":
            msg = "emails"
            op  = self.OP_EMAIL_RESTORE
        
        LOG.critical("Restore %s in gmail account %s." % (msg, self.login) ) 
        
        LOG.critical("Read %s info from %s gmvault-db." % (msg, self.db_root_dir))
        
        LOG.critical("Total number of %s to restore %s." % (msg, len(list(db_gmail_ids_info.keys()))))
        
        if restart:
            db_gmail_ids_info = self.get_gmails_ids_left_to_restore(op, db_gmail_ids_info)
        
        total_nb_emails_to_restore = len(db_gmail_ids_info)
        LOG.critical("Got all %s id left to restore. Still %s %s to do.\n" % (msg, total_nb_emails_to_restore, msg) )
        
        existing_labels = set() #set of existing labels to not call create_gmail_labels all the time
        nb_emails_restored = 0 #to count nb of emails restored
        timer = gmvault_utils.Timer() # needed for enhancing the user information
        timer.start()
        
        for gm_id in db_gmail_ids_info:
            
            LOG.critical("Restore %s with id %s." % (msg, gm_id))
            
            email_meta, email_data = self.unbury_email(gm_id)
            
            LOG.debug("Unburied %s with id %s." % (msg, gm_id))
            
            #labels for this email => real_labels U extra_labels
            labels = set(email_meta[self.gstorer.LABELS_K])
            labels = labels.union(extra_labels)
            
            # get list of labels to create 
            labels_to_create = [ label for label in labels if label not in existing_labels]
            
            #create the non existing labels
            if len(labels_to_create) > 0:
                LOG.debug("Labels creation tentative for %s with id %s." % (msg, gm_id))
                existing_labels = self.src.create_gmail_labels(labels_to_create, existing_labels)
            
            try:
                #restore email
                self.src.push_email(email_data, \
                                    email_meta[self.gstorer.FLAGS_K] , \
                                    email_meta[self.gstorer.INT_DATE_K], \
                                    labels)
                
                LOG.debug("Pushed %s with id %s." % (msg, gm_id))
                
                nb_emails_restored += 1
                
                #indicate every 10 messages the number of messages left to process
                left_emails = (total_nb_emails_to_restore - nb_emails_restored)
                
                if (nb_emails_restored % 50) == 0 and (left_emails > 0): 
                    elapsed = timer.elapsed() #elapsed time in seconds
                    LOG.critical("\n== Processed %d %s in %s. %d left to be restored (time estimate %s).==\n" % \
                                 (nb_emails_restored, msg, timer.seconds_to_human_time(elapsed), \
                                  left_emails, timer.estimate_time_left(nb_emails_restored, elapsed, left_emails)))
                
                # save id every 20 restored emails
                if (nb_emails_restored % 10) == 0:
                    self.save_lastid(self.OP_CHAT_RESTORE, gm_id)
                    
            except imaplib.IMAP4.abort as abort:
                
                # if this is a Gmvault SSL Socket error quarantine the email and continue the restore
                if str(abort).find("=> Gmvault ssl socket error: EOF") >= 0:
                    LOG.critical("Quarantine %s with gm id %s from %s. "\
                                 "GMAIL IMAP cannot restore it: err={%s}" % (msg, gm_id, db_gmail_ids_info[gm_id], str(abort)))
                    self.gstorer.quarantine_email(gm_id)
                    self.error_report['emails_in_quarantine'].append(gm_id)
                    LOG.critical("Disconnecting and reconnecting to restart cleanly.")
                    self.src.reconnect() #reconnect
                else:
                    raise abort
        
            except imaplib.IMAP4.error as err:
                
                LOG.error("Catched IMAP Error %s" % (str(err)))
                LOG.exception(err)
                
                #When the email cannot be read from Database because it was empty when returned by gmail imap
                #quarantine it.
                if str(err) == "APPEND command error: BAD ['Invalid Arguments: Unable to parse message']":
                    LOG.critical("Quarantine %s with gm id %s from %s. GMAIL IMAP cannot restore it:"\
                                 " err={%s}" % (msg, gm_id, db_gmail_ids_info[gm_id], str(err)))
                    self.gstorer.quarantine_email(gm_id)
                    self.error_report['emails_in_quarantine'].append(gm_id) 
                else:
                    raise err
            except imap_utils.PushEmailError as p_err:
                LOG.error("Catch the following exception %s" % (str(p_err)))
                LOG.exception(p_err)
                
                if p_err.quarantined():
                    LOG.critical("Quarantine %s with gm id %s from %s. GMAIL IMAP cannot restore it:"\
                                 " err={%s}" % (msg, gm_id, db_gmail_ids_info[gm_id], str(p_err)))
                    self.gstorer.quarantine_email(gm_id)
                    self.error_report['emails_in_quarantine'].append(gm_id) 
                else:
                    raise p_err          
            except Exception as err:
                LOG.error("Catch the following exception %s" % (str(err)))
                LOG.exception(err)
                raise err
            
            
        return self.error_report 
        
    def old_restore_chats(self, extra_labels = [], restart = False): #pylint:disable=W0102
        """
           restore chats
        """
        LOG.critical("Restore chats in gmail account %s." % (self.login) ) 
                
        LOG.critical("Read chats info from %s gmvault-db." % (self.db_root_dir))
        
        #for the restore (save last_restored_id in .gmvault/last_restored_id
        
        #get gmail_ids from db
        db_gmail_ids_info = self.gstorer.get_all_chats_gmail_ids()
        
        LOG.critical("Total number of chats to restore %s." % (len(list(db_gmail_ids_info.keys()))))
        
        if restart:
            db_gmail_ids_info = self.get_gmails_ids_left_to_restore(self.OP_CHAT_RESTORE, db_gmail_ids_info)
        
        total_nb_emails_to_restore = len(db_gmail_ids_info)
        LOG.critical("Got all chats id left to restore. Still %s chats to do.\n" % (total_nb_emails_to_restore) )
        
        existing_labels = set() #set of existing labels to not call create_gmail_labels all the time
        nb_emails_restored = 0 #to count nb of emails restored
        timer = gmvault_utils.Timer() # needed for enhancing the user information
        timer.start()
        
        for gm_id in db_gmail_ids_info:
            
            LOG.critical("Restore chat with id %s." % (gm_id))
            
            email_meta, email_data = self.gstorer.unbury_email(gm_id)
            
            LOG.debug("Unburied chat with id %s." % (gm_id))
            
            #labels for this email => real_labels U extra_labels
            labels = set(email_meta[self.gstorer.LABELS_K])
            labels = labels.union(extra_labels)
            
            # get list of labels to create 
            labels_to_create = [ label for label in labels if label not in existing_labels]
            
            #create the non existing labels
            if len(labels_to_create) > 0:
                LOG.debug("Labels creation tentative for chat with id %s." % (gm_id))
                existing_labels = self.src.create_gmail_labels(labels_to_create, existing_labels)
            
            try:
                #restore email
                self.src.push_email(email_data, \
                                    email_meta[self.gstorer.FLAGS_K] , \
                                    email_meta[self.gstorer.INT_DATE_K], \
                                    labels)
                
                LOG.debug("Pushed chat with id %s." % (gm_id))
                
                nb_emails_restored += 1
                
                #indicate every 10 messages the number of messages left to process
                left_emails = (total_nb_emails_to_restore - nb_emails_restored)
                
                if (nb_emails_restored % 50) == 0 and (left_emails > 0): 
                    elapsed = timer.elapsed() #elapsed time in seconds
                    LOG.critical("\n== Processed %d chats in %s. %d left to be restored (time estimate %s).==\n" % \
                                 (nb_emails_restored, timer.seconds_to_human_time(elapsed), \
                                  left_emails, timer.estimate_time_left(nb_emails_restored, elapsed, left_emails)))
                
                # save id every 20 restored emails
                if (nb_emails_restored % 10) == 0:
                    self.save_lastid(self.OP_CHAT_RESTORE, gm_id)
                    
            except imaplib.IMAP4.abort as abort:
                
                # if this is a Gmvault SSL Socket error quarantine the email and continue the restore
                if str(abort).find("=> Gmvault ssl socket error: EOF") >= 0:
                    LOG.critical("Quarantine email with gm id %s from %s. "\
                                 "GMAIL IMAP cannot restore it: err={%s}" % (gm_id, db_gmail_ids_info[gm_id], str(abort)))
                    self.gstorer.quarantine_email(gm_id)
                    self.error_report['emails_in_quarantine'].append(gm_id)
                    LOG.critical("Disconnecting and reconnecting to restart cleanly.")
                    self.src.reconnect() #reconnect
                else:
                    raise abort
        
            except imaplib.IMAP4.error as err:
                
                LOG.error("Catched IMAP Error %s" % (str(err)))
                LOG.exception(err)
                
                #When the email cannot be read from Database because it was empty when returned by gmail imap
                #quarantine it.
                if str(err) == "APPEND command error: BAD ['Invalid Arguments: Unable to parse message']":
                    LOG.critical("Quarantine email with gm id %s from %s. GMAIL IMAP cannot restore it:"\
                                 " err={%s}" % (gm_id, db_gmail_ids_info[gm_id], str(err)))
                    self.gstorer.quarantine_email(gm_id)
                    self.error_report['emails_in_quarantine'].append(gm_id) 
                else:
                    raise err
            except imap_utils.PushEmailError as p_err:
                LOG.error("Catch the following exception %s" % (str(p_err)))
                LOG.exception(p_err)
                
                if p_err.quarantined():
                    LOG.critical("Quarantine email with gm id %s from %s. GMAIL IMAP cannot restore it:"\
                                 " err={%s}" % (gm_id, db_gmail_ids_info[gm_id], str(p_err)))
                    self.gstorer.quarantine_email(gm_id)
                    self.error_report['emails_in_quarantine'].append(gm_id) 
                else:
                    raise p_err          
            except Exception as err:
                LOG.error("Catch the following exception %s" % (str(err)))
                LOG.exception(err)
                raise err
            
            
        return self.error_report
    
    
    def restore_chats(self, extra_labels = [], restart = False): #pylint:disable=W0102
        """
           restore chats
        """
        LOG.critical("Restore chats in gmail account %s." % (self.login) ) 
                
        LOG.critical("Read chats info from %s gmvault-db." % (self.db_root_dir))
        
        #for the restore (save last_restored_id in .gmvault/last_restored_id
        
        #get gmail_ids from db
        db_gmail_ids_info = self.gstorer.get_all_chats_gmail_ids()
        
        LOG.critical("Total number of chats to restore %s." % (len(list(db_gmail_ids_info.keys()))))
        
        if restart:
            db_gmail_ids_info = self.get_gmails_ids_left_to_restore(self.OP_CHAT_RESTORE, db_gmail_ids_info)
        
        total_nb_emails_to_restore = len(db_gmail_ids_info)
        LOG.critical("Got all chats id left to restore. Still %s chats to do.\n" % (total_nb_emails_to_restore) )
        
        existing_labels     = set() #set of existing labels to not call create_gmail_labels all the time
        nb_emails_restored  = 0  #to count nb of emails restored
        labels_to_apply     = collections_utils.SetMultimap()

        #get all mail folder name
        all_mail_name = self.src.get_folder_name("ALLMAIL")
        
        # go to DRAFTS folder because if you are in ALL MAIL when uploading emails it is very slow
        folder_def_location = gmvault_utils.get_conf_defaults().get("General","restore_default_location", "DRAFTS")
        self.src.select_folder(folder_def_location)
        
        timer = gmvault_utils.Timer() # local timer for restore emails
        timer.start()
        
        nb_items = gmvault_utils.get_conf_defaults().get_int("General","nb_messages_per_restore_batch", 100) 
        
        for group_imap_ids in itertools.zip_longest(fillvalue=None, *[iter(db_gmail_ids_info)]*nb_items): 

            last_id = group_imap_ids[-1] #will be used to save the last id
            #remove all None elements from group_imap_ids
            group_imap_ids = filter(lambda x: x != None, group_imap_ids)
           
            labels_to_create    = set() #create label set
            labels_to_create.update(extra_labels) # add extra labels to applied to all emails
            
            LOG.critical("Pushing the chats content of the current batch of %d emails.\n" % (nb_items))
            
            # unbury the metadata for all these emails
            for gm_id in group_imap_ids:    
                email_meta, email_data = self.gstorer.unbury_email(gm_id)
                
                LOG.critical("Pushing chat content with id %s." % (gm_id))
                LOG.debug("Subject = %s." % (email_meta[self.gstorer.SUBJECT_K]))
                try:
                    # push data in gmail account and get uids
                    imap_id = self.src.push_data(all_mail_name, email_data, \
                                    email_meta[self.gstorer.FLAGS_K] , \
                                    email_meta[self.gstorer.INT_DATE_K] )      
                
                    #labels for this email => real_labels U extra_labels
                    labels = set(email_meta[self.gstorer.LABELS_K])
                    
                    # add in the labels_to_create struct
                    for label in labels:
                        LOG.debug("label = %s\n" % (label))
                        labels_to_apply[str(label)] = imap_id
            
                    # get list of labels to create (do a union with labels to create)
                    labels_to_create.update([ label for label in labels if label not in existing_labels])                  
                
                except Exception as err:
                    handle_restore_imap_error(err, gm_id, db_gmail_ids_info, self)

            #create the non existing labels and update existing labels
            if len(labels_to_create) > 0:
                LOG.debug("Labels creation tentative for chat with id %s." % (gm_id))
                existing_labels = self.src.create_gmail_labels(labels_to_create, existing_labels)
                
            # associate labels with emails
            LOG.critical("Applying labels to the current batch of %d emails" % (nb_items))
            try:
                LOG.debug("Changing directory. Going into ALLMAIL")
                self.src.select_folder('ALLMAIL') #go to ALL MAIL to make STORE usable
                for label in list(labels_to_apply.keys()):
                    self.src.apply_labels_to(labels_to_apply[label], [label]) 
            except Exception as err:
                LOG.error("Problem when applying labels %s to the following ids: %s" %(label, labels_to_apply[label]), err)
                if isinstance(err, imaplib.IMAP4.abort) and str(err).find("=> Gmvault ssl socket error: EOF") >= 0:
                    # if this is a Gmvault SSL Socket error quarantine the email and continue the restore
                    LOG.critical("Quarantine email with gm id %s from %s. GMAIL IMAP cannot restore it:"\
                         " err={%s}" % (gm_id, db_gmail_ids_info[gm_id], str(err)))
                    self.gstorer.quarantine_email(gm_id)
                    self.error_report['emails_in_quarantine'].append(gm_id)
                    LOG.critical("Disconnecting and reconnecting to restart cleanly.")
                    self.src.reconnect() #reconnect
                else:
                    raise err
            finally:
                self.src.select_folder(folder_def_location) # go back to an empty DIR (Drafts) to be fast
                labels_to_apply = collections_utils.SetMultimap() #reset label to apply
            
            nb_emails_restored += nb_items
                
            #indicate every 10 messages the number of messages left to process
            left_emails = (total_nb_emails_to_restore - nb_emails_restored)
            
            if (left_emails > 0): 
                elapsed = timer.elapsed() #elapsed time in seconds
                LOG.critical("\n== Processed %d chats in %s. %d left to be restored "\
                             "(time estimate %s).==\n" % \
                             (nb_emails_restored, timer.seconds_to_human_time(elapsed), \
                              left_emails, timer.estimate_time_left(nb_emails_restored, elapsed, left_emails)))
            
            # save id every nb_items restored emails
            # add the last treated gm_id
            self.save_lastid(self.OP_EMAIL_RESTORE, last_id)
            
        return self.error_report 
                    
    def restore_emails(self, pivot_dir = None, extra_labels = [], restart = False):
        """
           restore emails in a gmail account using batching to group restore
           If you are not in "All Mail" Folder, it is extremely fast to push emails.
           But it is not possible to reapply labels if you are not in All Mail because the uid which is returned
           is dependant on the folder. On the other hand, you can restore labels in batch which would help gaining lots of time.
           The idea is to get a batch of 50 emails and push them all in the mailbox one by one and get the uid for each of them.
           Then create a dict of labels => uid_list and for each label send a unique store command after having changed dir
        """
        LOG.critical("Restore emails in gmail account %s." % (self.login) ) 
        
        LOG.critical("Read email info from %s gmvault-db." % (self.db_root_dir))
        
        #get gmail_ids from db
        db_gmail_ids_info = self.gstorer.get_all_existing_gmail_ids(pivot_dir)
        
        LOG.critical("Total number of elements to restore %s." % (len(list(db_gmail_ids_info.keys()))))
        
        if restart:
            db_gmail_ids_info = self.get_gmails_ids_left_to_restore(self.OP_EMAIL_RESTORE, db_gmail_ids_info)
        
        total_nb_emails_to_restore = len(db_gmail_ids_info)
        
        LOG.critical("Got all emails id left to restore. Still %s emails to do.\n" % (total_nb_emails_to_restore) )
        
        existing_labels     = set() #set of existing labels to not call create_gmail_labels all the time
        nb_emails_restored  = 0  #to count nb of emails restored
        labels_to_apply     = collections_utils.SetMultimap()

        #get all mail folder name
        all_mail_name = self.src.get_folder_name("ALLMAIL")
        
        # go to DRAFTS folder because if you are in ALL MAIL when uploading emails it is very slow
        folder_def_location = gmvault_utils.get_conf_defaults().get("General","restore_default_location", "DRAFTS")
        self.src.select_folder(folder_def_location)
        
        timer = gmvault_utils.Timer() # local timer for restore emails
        timer.start()
        
        nb_items = gmvault_utils.get_conf_defaults().get_int("General","nb_messages_per_restore_batch", 5) 
        
        for group_imap_ids in itertools.zip_longest(fillvalue=None, *[iter(db_gmail_ids_info)]*nb_items): 
            
            last_id = group_imap_ids[-1] #will be used to save the last id
            #remove all None elements from group_imap_ids
            group_imap_ids = filter(lambda x: x != None, group_imap_ids)
           
            labels_to_create    = set() #create label set
            labels_to_create.update(extra_labels) # add extra labels to applied to all emails
            
            LOG.critical("Pushing the email content of the current batch of %d emails.\n" % (nb_items))
            
            # unbury the metadata for all these emails
            for gm_id in group_imap_ids:    
                email_meta, email_data = self.gstorer.unbury_email(gm_id)
                
                LOG.critical("Pushing email body with id %s." % (gm_id))
                LOG.debug("Subject = %s." % (email_meta[self.gstorer.SUBJECT_K]))
                try:
                    # push data in gmail account and get uids
                    imap_id = self.src.push_data(all_mail_name, email_data, \
                                    email_meta[self.gstorer.FLAGS_K] , \
                                    email_meta[self.gstorer.INT_DATE_K] )      
                
                    #labels for this email => real_labels U extra_labels
                    labels = set(email_meta[self.gstorer.LABELS_K])
                    
                    # add in the labels_to_create struct
                    for label in labels:
                        LOG.debug("label = %s\n" % (label))
                        labels_to_apply[str(label)] = imap_id
            
                    # get list of labels to create (do a union with labels to create)
                    labels_to_create.update([ label for label in labels if label not in existing_labels])                  
                
                except Exception as err:
                    handle_restore_imap_error(err, gm_id, db_gmail_ids_info, self)

            #create the non existing labels and update existing labels
            if len(labels_to_create) > 0:
                LOG.debug("Labels creation tentative for email with id %s." % (gm_id))
                existing_labels = self.src.create_gmail_labels(labels_to_create, existing_labels)
                
            # associate labels with emails
            LOG.critical("Applying labels to the current batch of %d emails" % (nb_items))
            try:
                LOG.debug("Changing directory. Going into ALLMAIL")
                t = gmvault_utils.Timer()
                t.start()
                self.src.select_folder('ALLMAIL') #go to ALL MAIL to make STORE usable
                LOG.debug("Changed dir. Operation time = %s ms" % (t.elapsed_ms()))
                for label in list(labels_to_apply.keys()):
                    self.src.apply_labels_to(labels_to_apply[label], [label]) 
            except Exception as err:
                LOG.error("Problem when applying labels %s to the following ids: %s" %(label, labels_to_apply[label]), err)
                if isinstance(err, imaplib.IMAP4.abort) and str(err).find("=> Gmvault ssl socket error: EOF") >= 0:
                    # if this is a Gmvault SSL Socket error quarantine the email and continue the restore
                    LOG.critical("Quarantine email with gm id %s from %s. GMAIL IMAP cannot restore it:"\
                         " err={%s}" % (gm_id, db_gmail_ids_info[gm_id], str(err)))
                    self.gstorer.quarantine_email(gm_id)
                    self.error_report['emails_in_quarantine'].append(gm_id)
                    LOG.critical("Disconnecting and reconnecting to restart cleanly.")
                    self.src.reconnect() #reconnect
                else:
                    raise err
            finally:
                self.src.select_folder(folder_def_location) # go back to an empty DIR (Drafts) to be fast
                labels_to_apply = collections_utils.SetMultimap() #reset label to apply
            
            nb_emails_restored += nb_items
                
            #indicate every 10 messages the number of messages left to process
            left_emails = (total_nb_emails_to_restore - nb_emails_restored)
            
            if (left_emails > 0): 
                elapsed = timer.elapsed() #elapsed time in seconds
                LOG.critical("\n== Processed %d emails in %s. %d left to be restored "\
                             "(time estimate %s).==\n" % \
                             (nb_emails_restored, timer.seconds_to_human_time(elapsed), \
                              left_emails, timer.estimate_time_left(nb_emails_restored, elapsed, left_emails)))
            
            # save id every 50 restored emails
            # add the last treated gm_id
            self.save_lastid(self.OP_EMAIL_RESTORE, last_id)
            
        return self.error_report 
    
    def old_restore_emails(self, pivot_dir = None, extra_labels = [], restart = False):
        """
           restore emails in a gmail account using batching to group restore
           If you are not in "All Mail" Folder, it is extremely fast to push emails.
           But it is not possible to reapply labels if you are not in All Mail because the uid which is returned
           is dependant on the folder. On the other hand, you can restore labels in batch which would help gaining lots of time.
           The idea is to get a batch of 50 emails and push them all in the mailbox one by one and get the uid for each of them.
           Then create a dict of labels => uid_list and for each label send a unique store command after having changed dir
        """
        LOG.critical("Restore emails in gmail account %s." % (self.login) ) 
        
        LOG.critical("Read email info from %s gmvault-db." % (self.db_root_dir))
        
        #get gmail_ids from db
        db_gmail_ids_info = self.gstorer.get_all_existing_gmail_ids(pivot_dir)
        
        LOG.critical("Total number of elements to restore %s." % (len(list(db_gmail_ids_info.keys()))))
        
        if restart:
            db_gmail_ids_info = self.get_gmails_ids_left_to_restore(self.OP_EMAIL_RESTORE, db_gmail_ids_info)
        
        total_nb_emails_to_restore = len(db_gmail_ids_info)
        
        LOG.critical("Got all emails id left to restore. Still %s emails to do.\n" % (total_nb_emails_to_restore) )
        
        existing_labels     = set() #set of existing labels to not call create_gmail_labels all the time
        nb_emails_restored  = 0  #to count nb of emails restored
        labels_to_apply     = collections_utils.SetMultimap()
        
        new_conn  = self.src.spawn_connection()
        job_queue = Queue()
        job_nb    = 1
        
        timer = gmvault_utils.Timer() # local timer for restore emails
        timer.start()
        
        labelling_thread = LabellingThread(group=None, target=None, name="LabellingThread", args=(), kwargs={"queue" : job_queue, \
                                                                                                             "conn" : new_conn, \
                                                                                                             "gmvaulter" : self, \
                                                                                                             "total_nb_emails_to_restore": total_nb_emails_to_restore, 
                                                                                                             "timer": timer}, \
                                           verbose=None)
        labelling_thread.start()

        #get all mail folder name
        all_mail_name = self.src.get_folder_name("ALLMAIL")
        
        # go to DRAFTS folder because if you are in ALL MAIL when uploading emails it is very slow
        folder_def_location = gmvault_utils.get_conf_defaults().get("General","restore_default_location", "DRAFTS")
        self.src.select_folder(folder_def_location)
        
        nb_items = gmvault_utils.get_conf_defaults().get_int("General","nb_messages_per_restore_batch", 10) 
        
        for group_imap_ids in itertools.zip_longest(fillvalue=None, *[iter(db_gmail_ids_info)]*nb_items): 
            
            last_id = group_imap_ids[-1] #will be used to save the last id
            #remove all None elements from group_imap_ids
            group_imap_ids = filter(lambda x: x != None, group_imap_ids)
           
            labels_to_create    = set() #create label set
            labels_to_create.update(extra_labels) # add extra labels to applied to all emails
            
            LOG.critical("Pushing the email content of the current batch of %d emails.\n" % (nb_items))
            
            # unbury the metadata for all these emails
            for gm_id in group_imap_ids:    
                email_meta, email_data = self.gstorer.unbury_email(gm_id)
                
                LOG.critical("Pushing email body with id %s." % (gm_id))
                LOG.debug("Subject = %s." % (email_meta[self.gstorer.SUBJECT_K]))
                try:
                    # push data in gmail account and get uids
                    imap_id = self.src.push_data(all_mail_name, email_data, \
                                    email_meta[self.gstorer.FLAGS_K] , \
                                    email_meta[self.gstorer.INT_DATE_K] )      
                
                    #labels for this email => real_labels U extra_labels
                    labels = set(email_meta[self.gstorer.LABELS_K])
                    
                    # add in the labels_to_create struct
                    for label in labels:
                        LOG.debug("label = %s\n" % (label))
                        labels_to_apply[str(label)] = imap_id
            
                    # get list of labels to create (do a union with labels to create)
                    labels_to_create.update([ label for label in labels if label not in existing_labels])                  
                
                except Exception as err:
                    handle_restore_imap_error(err, gm_id, db_gmail_ids_info, self)

            #create the non existing labels and update existing labels
            if len(labels_to_create) > 0:
                LOG.debug("Labels creation tentative for email with id %s." % (gm_id))
                existing_labels = self.src.create_gmail_labels(labels_to_create, existing_labels)
                
            job_queue.put(LabelJob(1, "LabellingJob-%d" % (job_nb), labels_to_apply , last_id, nb_items, None))
            job_nb +=1
            labels_to_apply = collections_utils.SetMultimap()   
            
        return self.error_report 


class LabelJob(object):
    def __init__(self, priority, name, labels_to_create, last_id, nb_items, imapid_gmid_map):
        self.priority           = priority
        self.labels             = labels_to_create
        self.nb_items           = nb_items
        self.last_id            = last_id
        self.imap_to_gm         = imapid_gmid_map
        self.name               = name
        return
    
    def type(self):
        return "LABELJOB"
    
    def __cmp__(self, other):
        return cmp(self.priority, other.priority)

class StopJob(object):
    def __init__(self, priority):
        self.priority    = priority
        return
    
    def type(self):
        return "STOPJOB"
    
    def __cmp__(self, other):
        return cmp(self.priority, other.priority)
    
class LabellingThread(Process):

    def __init__(self, group=None, target=None, name=None,
                 args=(), kwargs=None, verbose=None):
        
        Process.__init__(self)
        self.args                       = args
        self.kwargs                     = kwargs
        self.queue                      = kwargs.get("queue", None)
        self.src                        = kwargs.get("conn", None)
        self.gmvaulter                  = kwargs.get("gmvaulter", None)
        self.total_nb_emails_to_restore = kwargs.get("total_nb_emails_to_restore", None)
        self.timer                      = kwargs.get("timer", None)
        self.nb_emails_restored = 0

    def run(self):
        
        """
           Listen to the queue
           When job label, apply labels to emails and save last_id
           If error quarantine it and continue (if 15 consecutive errors stop).
        """
        LOG.debug("Labelling Thread. Changing directory. Going into ALLMAIL")
        folder_def_location = gmvault_utils.get_conf_defaults().get("General","restore_default_location", "DRAFTS")
        t = gmvault_utils.Timer()
        t.start()
        self.src.select_folder('ALLMAIL') #go to ALL MAIL to make STORE usable
        LOG.debug("Changed dir. Operation time = %s ms" % (t.elapsed_ms()))
        
        running = True
        while running:
            job =self.queue.get(block = True, timeout = None)
            
            
            LOG.critical("==== (LabellingThread) ====. Received job %s ====" % (job.name))
            
            if job.type() == "LABELJOB":
                # associate labels with emails
                labels_to_apply = job.labels
                imap_to_gm      = job.imap_to_gm
                
                #LOG.critical("Applying labels to the current batch of %d emails" % (job.nb_items))
                try:
                    
                    #for i in range(1,10):
                    #   LOG.critical("Hello")
                    #   #time.sleep(1)
                    for label in list(labels_to_apply.keys()):
                        LOG.critical("Apply %s to %s" % (label, labels_to_apply[label]))
                        self.src.apply_labels_to(labels_to_apply[label], [label]) 
                except Exception as err:
                    LOG.error("Problem when applying labels %s to the following ids: %s" %(label, labels_to_apply[label]), err)
                finally:
                    #self.queue.task_done()
                    pass
                
                #to be moved
                self.nb_emails_restored += job.nb_items
                
                #indicate every 10 messages the number of messages left to process
                left_emails = (self.total_nb_emails_to_restore - self.nb_emails_restored)
            
                if (left_emails > 0): 
                    elapsed = self.timer.elapsed() #elapsed time in seconds
                    LOG.critical("\n== Processed %d emails in %s. %d left to be restored "\
                             "(time estimate %s).==\n" % \
                             (self.nb_emails_restored, self.timer.seconds_to_human_time(elapsed), \
                              left_emails, self.timer.estimate_time_left(self.nb_emails_restored, elapsed, left_emails)))
            
                # save id every 50 restored emails
                # add the last treated gm_id
                self.gmvaulter.save_lastid(GMVaulter.OP_EMAIL_RESTORE, job.last_id)
            
            elif job.type() == "STOPJOB":
                self.queue.task_done()
                running = False
            
            #self.src.select_folder(folder_def_location)
            LOG.critical("==== (LabellingThread) ====. End of job %s ====" % (job.name))
        
