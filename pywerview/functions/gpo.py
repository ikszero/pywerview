# -*- coding: utf8 -*-

# This file is part of PywerView.

# PywerView is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# PywerView is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with PywerView.  If not, see <http://www.gnu.org/licenses/>.

# Yannick Méheut [yannick (at) meheut (dot) org] - Copyright © 2016

import codecs
from bs4 import BeautifulSoup
from StringIO import StringIO

from impacket.smbconnection import SMBConnection, SessionError

from pywerview.objects.adobjects import *
from pywerview.requester import LDAPRequester
from pywerview.functions.net import NetRequester

class GPORequester(LDAPRequester):

    @LDAPRequester._ldap_connection_init
    def get_netgpo(self, queried_gponame='*', queried_displayname=str(),
                   queried_domain=str(), ads_path=str()):

        gpo_search_filter = '(objectCategory=groupPolicyContainer)'

        if queried_displayname:
            gpo_search_filter += '(displayname={})'.format(queried_displayname)
        else:
            gpo_search_filter += '(name={})'.format(queried_gponame)

        gpo_search_filter = '(&{})'.format(gpo_search_filter)

        return self._ldap_search(gpo_search_filter, GPO)

    def get_gpttmpl(self, gpttmpl_path):
        content_io = StringIO()

        gpttmpl_path_split = gpttmpl_path.split('\\')
        target = gpttmpl_path_split[2]
        share = gpttmpl_path_split[3]
        file_name = '\\'.join(gpttmpl_path_split[4:])

        smb_connection = SMBConnection(remoteName=target, remoteHost=target)
        # TODO: kerberos login
        smb_connection.login(self._user, self._password, self._domain,
                             self._lmhash, self._nthash)

        smb_connection.connectTree(share)
        smb_connection.getFile(share, file_name, content_io.write)

        content = codecs.decode(content_io.getvalue(), 'utf_16_le')[1:].replace('\r', '')

        gpttmpl_final = GptTmpl(list())
        for l in content.split('\n'):
            if l.startswith('['):
                section_name = l.strip('[]').replace(' ', '').lower()
                setattr(gpttmpl_final, section_name, Policy(list()))
            elif '=' in l:
                property_name, property_values = [x.strip() for x in l.split('=')]
                if ',' in property_values:
                    property_values = property_values.split(',')
                setattr(getattr(gpttmpl_final, section_name), property_name, property_values)

        return gpttmpl_final

    def get_domainpolicy(self, source='domain', queried_domain=str(),
                         resolve_sids=False):
        if source == 'domain':
            queried_gponame = '{31B2F340-016D-11D2-945F-00C04FB984F9}'
        elif source == 'dc':
            queried_gponame = '{6AC1786C-016F-11D2-945F-00C04FB984F9}'
        gpo = self.get_netgpo(queried_domain=queried_domain, queried_gponame=queried_gponame)[0]

        gpttmpl_path = '{}\\MACHINE\\Microsoft\\Windows NT\\SecEdit\\GptTmpl.inf'.format(gpo.gpcfilesyspath)
        gpttmpl = self.get_gpttmpl(gpttmpl_path)

        if source == 'domain':
            return gpttmpl
        elif source == 'dc':
            if not resolve_sids:
                return gpttmpl
            else:
                import inspect
                try:
                    privilege_rights_policy = gpttmpl.privilegerights
                except AttributeError:
                    return gpttmpl

                members = inspect.getmembers(privilege_rights_policy, lambda x: not(inspect.isroutine(x)))
                with NetRequester(self._domain_controller, self._domain, self._user,
                                  self._password, self._lmhash, self._nthash) as net_requester:
                    for member in members:
                        if member[0].startswith('_'):
                            continue
                        if not isinstance(member[1], list):
                            sids = [member[1]]
                        else:
                            sids = member[1]
                        resolved_sids = list()
                        for sid in sids:
                            try:
                                resolved_sid = net_requester.get_adobject(queried_sid=sid)[0]
                            except IndexError:
                                resolved_sid = sid
                            else:
                                resolved_sid = resolved_sid.distinguishedname.split(',')[:2]
                                resolved_sid = '{}\\{}'.format(resolved_sid[1], resolved_sid[0])
                                resolved_sid = resolved_sid.replace('CN=', '')
                                resolved_sids.append(resolved_sid)
                        if len(resolved_sids) == 1:
                            resolved_sids = resolved_sids[0]
                        setattr(privilege_rights_policy, member[0], resolved_sids)

                gpttmpl.privilegerights = privilege_rights_policy

                return gpttmpl

    def _get_groupsxml(self, groupsxml_path, gpo_display_name):
        gpo_groups = list()

        content_io = StringIO()

        groupsxml_path_split = groupsxml_path.split('\\')
        gpo_name = groupsxml_path_split[6]
        target = groupsxml_path_split[2]
        share = groupsxml_path_split[3]
        file_name = '\\'.join(groupsxml_path_split[4:])

        smb_connection = SMBConnection(remoteName=target, remoteHost=target)
        # TODO: kerberos login
        smb_connection.login(self._user, self._password, self._domain,
                             self._lmhash, self._nthash)

        smb_connection.connectTree(share)
        try:
            smb_connection.getFile(share, file_name, content_io.write)
        except SessionError:
            return list()

        content = content_io.getvalue().replace('\r', '')
        groupsxml_soup = BeautifulSoup(content, 'xml')

        for group in groupsxml_soup.find_all('Group'):
            members = list()
            memberof = list()
            local_sid = group.Properties.get('groupSid', str())
            if not local_sid:
                if 'administrators' in group.Properties['groupName'].lower():
                    local_sid = 'S-1-5-32-544'
                elif 'remote desktop' in group.Properties['groupName'].lower():
                    local_sid = 'S-1-5-32-555'
                else:
                    local_sid = group.Properties['groupName']
            memberof.append(local_sid)

            for member in group.Properties.find_all('Member'):
                if not member['action'].lower() == 'add':
                    continue
                if member['sid']:
                    members.append(member['sid'])
                else:
                    members.append(member['name'])

            if members or memberof:
                # TODO: implement filter support (seems like a pain in the ass,
                # I'll do it if the feature is asked). PowerView also seems to
                # have the barest support for filters, so ¯\_(ツ)_/¯

                gpo_group = GPOGroup(list())
                setattr(gpo_group, 'gpodisplayname', gpo_display_name)
                setattr(gpo_group, 'gponame', gpo_name)
                setattr(gpo_group, 'gpopath', groupsxml_path)
                setattr(gpo_group, 'members', members)
                setattr(gpo_group, 'memberof', memberof)

                gpo_groups.append(gpo_group)

        return gpo_groups

    def _get_groupsgpttmpl(self, gpttmpl_path, gpo_display_name):
        import inspect
        gpo_groups = list()

        gpt_tmpl = self.get_gpttmpl(gpttmpl_path)
        gpo_name = gpttmpl_path.split('\\')[6]

        try:
            group_membership = gpt_tmpl.groupmembership
        except AttributeError:
            return list()

        membership = inspect.getmembers(group_membership, lambda x: not(inspect.isroutine(x)))
        for m in membership:
            if not m[1]:
                continue
            members = list()
            memberof = list()
            if m[0].lower().endswith('__memberof'):
                members.append(m[0].upper().lstrip('*').replace('__MEMBEROF', ''))
                if not isinstance(m[1], list):
                    memberof_list = [m[1]]
                memberof += [x.lstrip('*') for x in memberof_list]
            elif m[0].lower().endswith('__members'):
                memberof.append(m[0].upper().lstrip('*').replace('__MEMBERS', ''))
                if not isinstance(m[1], list):
                    members_list = [m[1]]
                members += [x.lstrip('*') for x in members_list]

            if members and memberof:
                gpo_group = GPOGroup(list())
                setattr(gpo_group, 'gpodisplayname', gpo_display_name)
                setattr(gpo_group, 'gponame', gpo_name)
                setattr(gpo_group, 'gpopath', gpttmpl_path)
                setattr(gpo_group, 'members', members)
                setattr(gpo_group, 'memberof', memberof)

                gpo_groups.append(gpo_group)

        return gpo_groups

    def get_netgpogroup(self, queried_gponame='*', queried_displayname=str(),
                        queried_domain=str(), ads_path=str(), resolve_sids=False):
        results = list()
        gpos = self.get_netgpo(queried_gponame=queried_gponame,
                               queried_displayname=queried_displayname,
                               queried_domain=queried_domain,
                               ads_path=ads_path)

        for gpo in gpos:
            gpo_display_name = gpo.displayname

            groupsxml_path = '{}\\MACHINE\\Preferences\\Groups\\Groups.xml'.format(gpo.gpcfilesyspath)
            gpttmpl_path = '{}\\MACHINE\\Microsoft\\Windows NT\\SecEdit\\GptTmpl.inf'.format(gpo.gpcfilesyspath)

            results += self._get_groupsxml(groupsxml_path, gpo_display_name)
            results += self._get_groupsgpttmpl(gpttmpl_path, gpo_display_name)

        if resolve_sids:
            for gpo_group in results:
                members = gpo_group.members
                memberof = gpo_group.memberof

                resolved_members = list()
                resolved_memberof = list()
                with NetRequester(self._domain_controller, self._domain, self._user,
                                  self._password, self._lmhash, self._nthash) as net_requester:
                    for member in members:
                        try:
                            resolved_member = net_requester.get_adobject(queried_sid=member)[0]
                            resolved_member = resolved_member.distinguishedname.split(',')
                            resolved_member_domain = '.'.join(resolved_member[1:])
                            resolved_member = '{}\\{}'.format(resolved_member_domain, resolved_member[0])
                            resolved_member = resolved_member.replace('CN=', '').replace('DC=', '')
                        except IndexError:
                            resolved_member = member
                        finally:
                            resolved_members.append(resolved_member)
                    gpo_group.members = resolved_members

                    for member in memberof:
                        try:
                            resolved_member = net_requester.get_adobject(queried_sid=member)[0]
                            resolved_member = resolved_member.distinguishedname.split(',')[:2]
                            resolved_member = '{}\\{}'.format(resolved_member[1], resolved_member[0])
                            resolved_member = resolved_member.replace('CN=', '').replace('DC=', '')
                        except IndexError:
                            resolved_member = member
                        finally:
                            resolved_memberof.append(resolved_member)
                    gpo_group.memberof = memberof = resolved_memberof

        return results

